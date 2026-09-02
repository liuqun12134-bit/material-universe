# Seedream 异步图片接口边界

用户提供的接入说明确认：

- API Base：`https://kaiyuncode.com`
- 提交：`POST /v1/images/async/generations`
- 查询：`GET /v1/images/async/{task_id}`
- 模型：`seedream-5.0-pro`
- 提交后保存 `task_id`，建议每 5 秒查询一次。
- `status=completed` 后从 `data[].url` 读取图片。
- `status=failed` 时停止轮询并报告错误。
- 已确认请求字段：`model`、`prompt`、`n`、`size`、`aspect_ratio`。

接入说明的 cURL 示例仍写成 `firefly-image-5`，而且没有列出参考图字段。当前 Seedream 适配器把常见参考图协议做成注册配置：

```json
{
  "reference_field": "image",
  "reference_shape": "array",
  "reference_encoding": "data_uri"
}
```

因此当前发送形式为：

```json
{
  "model": "seedream-5.0-pro",
  "prompt": "...",
  "n": 1,
  "size": "2048x2048",
  "image": ["data:image/png;base64,..."]
}
```

此参考图字段尚未由用户提供的文档证实。正式付费调用前应先运行 `--dry-run` 核对；若平台实际要求其他字段，只修改 `references/models.json` 中 Seedream 的 `options` 或对应适配器，不修改预处理主流程。
