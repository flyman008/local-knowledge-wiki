# 媒体入库与保留（retention-v1）

## 分工与验收

客户端实际理解媒体，再 prepared 提交原件、解析稿、知识稿；服务只登记、共享存储、试压和清理，不负责语义验收，不调用后台模型。不因本机有 ffmpeg 就声称已完成转写：它能抽帧/提取音轨，不等于理解内容。缺客户端视觉/音频能力时，标明缺失并保留原件；不得静默调用付费API。

- 图片：保留位置和含义，表格/小字实际核对。服务只做字节级去重，不合并“看起来相似”的图；无损试压逐像素、尺寸及ICC/EXIF/XMP校验，至少省5%才使用。不降分辨率，不做有损压缩。
- 视频：完整覆盖语音和重要画面；保存带时间戳转写、必要的说话人标注、关键帧与画面说明。无语音也应在时间线如实写明。只读字幕不等于读完视频。
- 音频：保存带时间戳转写及必要的说话人信息；听不清就标明，不能猜。
- GIF/多帧图片：按动态媒体处理，保存有序关键帧和动作/变化说明，不可只看首帧。
- 图片、转写、关键帧、来源、原材料日期长期保留。视频/音频/动图原件只有可回访且验收齐备才能清理。链接未来可能失效，不承诺恢复。
- PDF/PPT/Word/Excel不拆改。嵌套ZIP不展开，作为原字节保留。仅本地唯一原件不清理。

## 经共用CLI执行

prepared 保存并回读以后，含独立媒体或ZIP时执行：

```
py -3.12 D:/Knowledge/_service/intake.py media inventory --receipt <回执>
```

用服务返回的 `name` 和哈希编清单，不能猜文件名。清单精确覆盖 inventory 中所有非 other 条目（图片/视频/音频/动图）；other 自动按原字节保留。图片解读引用至少12字，必须已在本次保存的解析稿中。原始哈希在无损转换后仍记录为原始身份。

JSON示例（字段值均应换成真实内容）：

```json
{
  "receipt_id": "rcp_...",
  "raw_sha256": "inventory的原件哈希",
  "checked_by": "当前客户端及实际解析方式",
  "source_url": "https://原件或采集包的真实来源页面",
  "refetchable": true,
  "source_checked_at": "2026-09-18T10:00:00+08:00",
  "items": [
    {
      "name": "assets/example.png",
      "sha256": "inventory中该图片哈希",
      "complete": true,
      "locator": "正文第2节图1",
      "quote": "已经存入解析稿的完整图片说明引用，至少十二字",
      "source_url": "https://该图片或所属文章来源"
    }
  ]
}
```

`refetchable` 是客户端实际检查来源后作出的声明，不是“有一个URL”；不能把本地路径或已失效网页填成可回访。source_checked_at 需含时区且在最近7天内。服务校验字段和引用，不访问外网、不独立保证可重新下载。

动态条目另外提供：`source_url`、`refetchable:true`、`timeline_checked:true`、`timeline_quote`（已存解析稿中的带时间定位文字，至少12字）。视频/动图再加 `visual_quote`（画面/动作说明引用）和 `keyframes:[{"name":"frames/001.png","seconds":0},{"name":"frames/002.png","seconds":2.5}]`，时间非负、严格递增。帧数按内容变化决定，不把首帧当覆盖证明。

关键帧可在原始ZIP中，也可另打一个只含所引用关键帧的ZIP，用 `--assets` 传给服务。不得通过JSON指定任意本地路径让服务读取。

```
py -3.12 D:/Knowledge/_service/intake.py media prepare --file <清单.json> --assets <可选关键帧.zip>
py -3.12 D:/Knowledge/_service/intake.py media status --receipt <回执>
```

`blocked` / 退出码3：未满足条件，原件不清理；补读内容需先走版本化prepared和已有review闭环，不能只把complete改true。`ready`：保留包已生成，但尚未排入清理。此时仍同时占用原件与共享对象空间，不报已节省磁盘。

用户已同意媒体保留策略的入库任务，在核对清单和保留包后可进入待清理期；用户仅要求存档/保留原件或未授权清理时，不stage。不得批量将历史材料套用声明。

```
py -3.12 D:/Knowledge/_service/intake.py media stage --receipt <回执>
py -3.12 D:/Knowledge/_service/intake.py media cancel --receipt <回执>
```

stage 后原件仍留在原位置，逻辑上进入7天待清理区；cancel立即取消且不删除保留包。服务运行期间每小时检查：到期后重核三层、解析稿哈希、保留对象哈希、未决事项、共享原件其他回执。任何条件不满足就暂缓，不靠重启才检查。服务停机时不会清理。已purged不能本地恢复原视频；需回访来源，不伪造恢复成功。

门户每条知识正文上方“媒体保存与空间管理”显示状态及精简包下载。精简包保留非媒体原字节、图片/关键帧和清单；图像文件名为保持文章引用可不变，实际编码及原始哈希在清单里记录。**精简包不是逐字节原件**。历史 saved_layers.raw 表示曾存入，当前可用性以媒体状态和原件下载结果为准。

最后报告：保留内容、清理/暂缓内容、恢复截止时间、未覆盖范围；没有真实转写/画面核验就不得stage。无需每篇新增人工审批，但业务冲突仍按原确认规则处理。
