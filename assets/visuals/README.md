# assets/visuals · 课件场景插画素材库

本目录供 `xhzh/visuals.py` 的 `scene_image()` 运行时解析，**只用目录（第一层）下的 `.jpg`**。

## 目录约定

```
assets/visuals/
├── market.jpg  farm.jpg  livestock.jpg  school.jpg  tool.jpg  travel.jpg   # 运行时使用
├── README.md                                                               # 本说明
└── src/                                                                    # 仅存档，运行时不读取
    ├── market.png  farm.png  livestock.png  school.png  tool.png  travel.png
```

| 项目 | 规格 | 说明 |
|---|---|---|
| 运行时 `.jpg` | 宽 1280、约 90~132KB/张 | 课件卡片实际嵌入的图，保证 6 张全用也只有 ~0.6MB，整包 ≤3MB（县乡低带宽预算） |
| `src/*.png` | 1280×854、1.5~1.8MB/张 | **AIGC 源图仅作存档**，不参与运行时解析。若误放入本目录第一层，`scene_image()` 的体积守卫（>400KB 自动降采样到 1280 宽 / JPEG q88 后缓存）会兜住，但仍建议留在 `src/` |
| 主题键 | `market` / `farm` / `livestock` / `school` / `tool` / `travel` | 对应 `xhzh.visuals.SCENE_THEMES`；缺失任意主题时该主题自动降级为矢量场景 |

## 合规与水印

- 图片由 AIGC 生成、经**人工过目**后入库，属"素材库"而非"实时生图"；运行时生图 API 默认关闭（`XHZH_IMAGE_ALLOW=1` 才启用）。
- 源图右下角带平台水印「AI生成 WORKBUDDY」。`visuals._add_picture_fill()` 内置水印裁切守卫（右/底各至少裁 9%，实测水印占 x≥0.92W、y≥0.94H），保证水印在任何目标框比例下都不会被投影出来。
- 课件中使用这些配图的页面，会在卡片右下角额外标注 `AI 生成配图`（8pt 灰字白底胶囊）。

## 更换素材的检查清单

1. 新图放到本目录第一层，命名 `{theme}.jpg`，宽 1280、JPEG q≈88；
2. 确认体积 < 300KB（`tests/test_suite.py` 有断言，>400KB 触发降采样守卫）；
3. 确认无第三方水印（若原图带水印，保留原图到 `src/`，运行图裁掉角部后再导出）；
4. **更换素材后必须用同一探针重新测量水印 bbox**：当前守卫 9% 与实测水印起点 92.3%W 之间仅余约 1.3%W（≈2mm），素材一旦左移即会泄漏。探针口径见下：
   ```python
   # 高亮白字检测：三通道均接近 255，且只统计右下角 85% 起的区域
   import numpy as np
   from PIL import Image
   a = np.asarray(Image.open("market.jpg").convert("RGB")).astype(int)
   h, w, _ = a.shape
   mask = (a[:, :, 0] >= 246) & (a[:, :, 1] >= 243) & (a[:, :, 2] >= 236)
   mask[: int(h * 0.85), :] = False
   mask[:, : int(w * 0.85)] = False
   ys, xs = np.nonzero(mask)
   print(f"水印 bbox x[{xs.min()/w:.3f},{xs.max()/w:.3f}] y[{ys.min()/h:.3f},{ys.max()/h:.3f}]")
   ```
   若测得起点左移（如 0.90W），须同步调大 `xhzh.visuals.WATERMARK_GUARD`；测试里的
   `WATERMARK_MIN_GUARD` 会拦住"把守卫调小"的回归；
5. 跑 `python tests/test_suite.py visuals` 复验。
