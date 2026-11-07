"""Model transport for the bounded Skill runner; translation transport is unchanged."""
import json
import re
import httpx
from fastapi import HTTPException

PLACEHOLDER = re.compile(r'\{\{(\w+)\}\}')


def expand(value, variables):
    if isinstance(value, str):
        match = PLACEHOLDER.fullmatch(value)
        if match:
            return variables.get(match[1], value)
        def substitute(match):
            item = variables.get(match[1], match[0])
            return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return PLACEHOLDER.sub(substitute, value)
    if isinstance(value, list):
        return [expand(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, variables) for k, v in value.items()}
    return value


async def invoke(service, config, messages, tools):
    headers = {'Content-Type': 'application/json'}
    if config.api_key.strip():
        headers['Authorization'] = 'Bearer ' + config.api_key.strip()
    if config.mode == 'openai':
        body = dict(model=config.model, messages=messages, stream=False)
        if tools:
            body.update(tools=tools, tool_choice='auto')
        if config.reasoning == 'off':
            body['thinking'] = {'type': 'disabled'}
        elif config.reasoning:
            body['reasoning_effort'] = config.reasoning
    else:
        protocol = ('返回 JSON：{"content":"Markdown 回答", "tool_calls":[]}；或 '
                    '{"content":"", "tool_calls":[{"id":"唯一ID","function":'
                    '{"name":"工具名","arguments":{"参数":"值"}}}]}。禁止同时回答和请求工具。')
        try:
            template = json.loads(config.template)
            images = [part for msg in messages if isinstance(msg.get('content'), list)
                      for part in msg['content'] if part.get('type') == 'image_url']
            if images and '{{images}}' not in config.template and '{{messages}}' not in config.template:
                raise HTTPException(422, '自定义接口看图需在 JSON 模板中使用 {{images}} 或 {{messages}} 结构化变量')
            variables = dict(model=config.model, target='简体中文', text='', images=images,
                             messages=messages, tools=tools)
            if '{{prompt}}' in config.template:
                variables['prompt'] = protocol + '\n' + json.dumps(dict(messages=messages, tools=tools), ensure_ascii=False)
            body = expand(template, variables)
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, '陪读请求模板必须是有效 JSON 对象')
    try:
        async with service.agent_client().stream('POST', service.endpoint(config), headers=headers,
                                                json=body, timeout=config.timeout) as response:
            if response.status_code >= 300:
                error_bytes = bytearray()
                async for chunk in response.aiter_bytes():
                    error_bytes.extend(chunk[:16384-len(error_bytes)])
                    if len(error_bytes) >= 16384:
                        break
                detail = error_bytes.decode('utf-8', errors='replace').lower()
                hint = {400: '接口拒绝工具或图片参数，请检查模型能力和思考设置',
                        401: '认证失败，请检查 API Key', 403: '接口拒绝访问',
                        404: '模型或接口不存在', 429: '接口限流或额度不足'}.get(response.status_code, '模型服务暂不可用')
                if response.status_code in (400, 413, 422):
                    if any(word in detail for word in ('too many images', 'image limit', 'maximum number of images')):
                        hint = '整篇输入超出当前模型或接口的图片数量限制，请调整多模态模型或接口后重试'
                    elif any(word in detail for word in ('context_length', 'context window', 'maximum context', 'token limit', 'too many tokens')):
                        hint = '整篇输入超出当前模型或接口的上下文长度限制，请调整模型上下文容量或接口后重试'
                    elif response.status_code == 413 or 'payload too large' in detail:
                        hint = '整篇输入超出当前接口的请求大小限制，请调整多模态模型或接口后重试'
                    elif any(word in detail for word in ('does not support image', 'unsupported image', 'image input is not supported', 'vision is not supported')):
                        hint = '当前模型或接口不支持图片输入，请选择支持图片的多模态模型后重试'
                raise HTTPException(502, hint)
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise HTTPException(502, '陪读模型返回内容过大')
        payload = json.loads(raw)
        if config.mode == 'openai':
            choice = payload['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise HTTPException(502, '回答被模型长度限制截断，请重试或缩小阅读范围')
            result = choice['message']
        else:
            result = payload
            for key in config.response_path.strip().split('.'):
                if key:
                    result = result[int(key)] if isinstance(result, list) else result[key]
            if isinstance(result, str):
                result = json.loads(result.removeprefix('```json').removesuffix('```').strip())
        if not isinstance(result, dict):
            raise ValueError()
        content, calls = result.get('content') or '', result.get('tool_calls') or []
        if not isinstance(content, str) or len(content) > 60000 or not isinstance(calls, list) or len(calls) > 12:
            raise ValueError()
        normalized = []
        for index, call in enumerate(calls):
            function = call['function']
            args = function['arguments']
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict) or not isinstance(function['name'], str):
                raise ValueError()
            normalized.append(dict(id=str(call.get('id') or f'call_{index}'), type='function',
                                   function=dict(name=function['name'], arguments=json.dumps(args, ensure_ascii=False))))
        if not content.strip() and not normalized:
            raise ValueError()
        return dict(role='assistant', content=content, tool_calls=normalized)
    except httpx.TimeoutException:
        raise HTTPException(504, '陪读模型请求超时，可重试')
    except httpx.RequestError:
        raise HTTPException(502, '无法连接陪读模型服务')
    except (ValueError, KeyError, TypeError, IndexError):
        raise HTTPException(502, '模型未返回有效陪读／工具协议，请测试接口能力')
