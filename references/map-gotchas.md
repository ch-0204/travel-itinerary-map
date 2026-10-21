# 地图层的老坑（改模板前必读）

模板里的坑都已经被填过了。**只有当你要动 `assets/template.html` 的脚本部分时才需要看这个**；
只改 `trip-data.json` 的话，跳过本文。

如果还需要更细的排查手法（Playwright 诊断三板斧、shim 兜底），见同级 skill
`tencent-map-route-html`（腾讯地图 GL JS 单页 HTML · 真实路线绘制）。

## 1. 坐标系：三个坐标系在这一页里同时存在

| 来源 | 坐标系 | 用途 |
|---|---|---|
| 腾讯地图 POI | GCJ-02 | 标点 `spots[].lat/lng`、`navData` 的点、`views.center` |
| OSRM / OSM | WGS-84 | 只用来算路，不能直接入库 |
| 用户给的经纬度 | 通常是 WGS-84（手机 GPS / 谷歌地图） | 入库前必须转 |

`scripts/route_tool.py` 已经处理好了这条链：
**GCJ-02 起点 → 转 WGS-84 → 问 OSRM → 结果转回 GCJ-02**。
漏掉任一方向都会偏 300–500m，线压在田里、标点落在河对岸。

只转单个点：`python scripts/route_tool.py --wgs2gcj 108.184,22.607`（注意是 `lng,lat`，经度在前）。

## 2. 图层必须「先挂上去，再藏起来」

```js
// ❌ 构造时 map:null，之后 setMap(map) 不会补建 GL 资源 —— 图层永远不显示，且不报错
var layer = new TMap.MultiPolyline({ map: null, styles: s, geometries: g });
layer.setMap(map);

// ✅ 先挂到地图上，再立刻藏起来（模板里所有图层都这么建）
var layer = new TMap.MultiPolyline({ map: map, styles: s, geometries: g });
try { layer.setMap(null); } catch (e) {}
```

这个坑最阴：老图层一切正常，只有新加的图层不显示，很容易误判成数据问题。

显示时补一次 `updateGeometries`，只 `setMap` 有时不刷新几何：

```js
function showLayer(layer, geoms, visible) {
  if (visible) { layer.setMap(map); layer.updateGeometries(geoms || []); }
  else { layer.setMap(null); }
}
```

## 3. `#map` 必须有 `z-index:0`，否则整页控件全部点不动

TMap 初始化后会往容器里注入一个 `position:absolute; z-index:1000` 的透明层。
容器若是 `z-index:auto`（不构成层叠上下文），这层会逃逸到父级去，把筛选条(120)、弹窗(300)
全盖住 —— 按钮看得见、能 hover、点下去毫无反应。

```css
#map { position:absolute; inset:0; z-index:0; }   /* 关住内部层级 */
.spot-marker { z-index:1001; }                     /* 自绘标点要压过注入层 */
```

## 4. 路线要有白描边

彩色线直接铺在底图上会糊。下面垫一层纯白（宽 9）、上面压彩色（宽 5），线才"浮"起来。
`color` 用 hex，`rgba()` 不一定被支持。

## 5. 标点用自绘 DOM + rAF，不用 DOMOverlay

`map.projectToContainer()` 每帧算一次位置，可靠性远高于依赖覆盖物生命周期。
低缩放（`zoom < 8`）时给 `#map` 加 `.zo-far` 类把图钉缩小，避免密集点位互相遮挡。

## 6. 别劫持 `window.TMap`

`TMap` 是分步填充的：`Object.defineProperty` 的 setter 只在 SDK 首次赋值时触发，那时
`MultiPolyline` 可能还没挂上，hook 无效甚至把页面搞崩。**验证渲染直接截图看图。**

## 7. 一定要留错误可视化

模板里已有：`window.onerror` + `reportErr()` + 轮询显示到 `#errbar`，外加左下角一行
「路网数据自检：N 段 / M 节点」。这行能让人一眼分清"数据没进去"和"图层没画出来"。

改交互函数时用 try/catch 包住并 `reportErr(...)`，否则出了问题页面静默失败。

## 8. file:// 直开的假象

占位符 `__WB_HTTP_PORT__` / `__WB_TMAP_SECRET__` 由预览服务替换。
`file://` 直开时底图和矢量图层**仍能渲染**，只是控制台会报一条
`XMLHttpRequest: Invalid URL` —— 这条不是你的 bug，别去修。要看真实效果走预览面板。
