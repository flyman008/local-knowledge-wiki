# 补读与核验闭环

客户端先对缺失部分补读一轮，服务不会唤醒客户端或调用付费视觉模型。仍无法读取时保留具体缺失及原因，不假装完成。

补读保存同原件的 prepared 新版本（correction_reason + base_version）后，客户端调用 `intake.py review repair --file repair.json`，传原事项 ID、最新 revision、新回执及逐页 coverage。完整参数见 Skill 的 references/confirmation.md。

服务核验同主题、紧邻版本、原件哈希、三层保存状态、无剩余缺失、解析稿引用覆盖缺失编号。通过后留事件记录并关闭对应技术事项；只有不存在其他业务待确认时，才一并关闭本次生成的版本关系事项。该检查不证明视觉理解正确或全文事实真实。可用 reopen-repair 重新打开，不能以此关闭业务争议。

原文未披露测试方法等信息可在知识稿保留限定，并用 limitation 登记为 notice；不计入人工待办，但检索与门户仍展示。此操作不能降级或关闭旧 pending，不能用于不同来源的价格、能力、版本冲突。自然语言来源策略不被服务机械执行为裁决。

portal 展示当前待处理、引用限制、旧版本仍未解决和已处理历史。新版不会遮掉旧未决项。没有批量清除历史问题，也没有部署客户端定时调度器。

验收：scripts/test_remediation.py 使用隔离文件/数据库，通过 HTTP 检查正反例、状态回读、事件和重新打开；scripts/test_correction.py / test_quality.py 回归。零真实模型调用。三端 Skill 静态同步不等于三个客户端均已实际补读验收。
