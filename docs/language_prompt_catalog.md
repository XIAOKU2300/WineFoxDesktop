# Zayu Common Message Catalog

`zayu_messages_template.json` 现在只维护常用用户提示，不再保存全量源码字符串扫描结果。

- 默认关闭：`zayu_mode_settings.enable = false`
- 开启后：只替换 `entries` 中配置的常用提示
- 默认路径：插件数据目录下的 `zayu_messages.json`
- 自定义路径：可在 `zayu_mode_settings.message_map_path` 中填写

为了避免误伤日志、正则、API 字符串和长 Prompt，杂鱼模式只建议维护短提示文案。
