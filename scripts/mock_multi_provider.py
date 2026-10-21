"""Deterministic local provider fixtures. Never use these credentials in production."""
import asyncio
import json
from collections import Counter

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
counts = Counter()


@app.get('/stats')
async def stats():
    return dict(counts)


@app.post('/{provider}/v1/{endpoint:path}')
async def generate(provider: str, endpoint: str, request: Request):
    expected = {'openai': 'upstream-openai-demo', 'deepseek': 'upstream-deepseek-demo', 'claude': 'upstream-claude-demo'}
    key = request.headers.get('x-api-key') if provider == 'claude' else request.headers.get('authorization', '').removeprefix('Bearer ')
    if key != expected.get(provider):
        return JSONResponse({'error': {'message': 'fixture credential mismatch'}}, status_code=401)
    counts[provider] += 1
    body = await request.json()
    text = json.dumps(body.get('messages', []))
    if 'FAIL_BEFORE' in text:
        return JSONResponse({'error': {'message': 'upstream-secret-sentinel'}}, status_code=503)
    tool = bool(body.get('tools'))
    usage = {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}
    if not body.get('stream'):
        if provider == 'claude':
            content = [{'type': 'tool_use', 'id': 'call_fixture', 'name': 'weather', 'input': {'city': 'Paris'}}] if tool else [{'type': 'text', 'text': 'Hello fixture'}]
            return {'id': 'fixture', 'type': 'message', 'role': 'assistant', 'model': body['model'], 'content': content, 'stop_reason': 'tool_use' if tool else 'end_turn', 'usage': {'input_tokens': 10, 'output_tokens': 3}}
        message = {'role': 'assistant', 'content': 'Hello fixture'}
        if tool:
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_fixture', 'type': 'function', 'function': {'name': 'weather', 'arguments': '{"city":"Paris"}'}}]}
        return {'id': 'fixture', 'object': 'chat.completion', 'model': body['model'], 'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if tool else 'stop'}], 'usage': usage}

    async def events():
        def frame(value, event=None):
            return ((f'event: {event}\n' if event else '') + 'data: ' + json.dumps(value) + '\n\n').encode()
        counts['active'] += 1
        try:
            if 'SLOW_PREFETCH' in text:
                await asyncio.sleep(5)
            if provider == 'claude':
                yield frame({'type': 'message_start', 'message': {'id': 'fixture', 'model': body['model'], 'usage': {'input_tokens': 10, 'output_tokens': 0}}}, 'message_start')
                block = {'type': 'tool_use', 'id': 'call_fixture', 'name': 'weather', 'input': {}} if tool else {'type': 'text', 'text': ''}
                yield frame({'type': 'content_block_start', 'index': 0, 'content_block': block}, 'content_block_start')
                delta = {'type': 'input_json_delta', 'partial_json': '{"city":"Paris"}'} if tool else {'type': 'text_delta', 'text': 'Hello fixture'}
                yield frame({'type': 'content_block_delta', 'index': 0, 'delta': delta}, 'content_block_delta')
            else:
                delta = {'tool_calls': [{'index': 0, 'id': 'call_fixture', 'type': 'function', 'function': {'name': 'weather', 'arguments': '{"city":"Paris"}'}}]} if tool else {'content': 'Hello fixture'}
                yield frame({'id': 'fixture', 'model': body['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})
            await asyncio.sleep(1 if 'SLOW' in text else 0.08)
            if 'FAIL_AFTER' in text:
                yield frame({'error': {'type': 'api_error', 'message': 'upstream-secret-sentinel'}})
                return
            if provider == 'claude':
                yield frame({'type': 'content_block_stop', 'index': 0}, 'content_block_stop')
                yield frame({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tool else 'end_turn'}, 'usage': {'output_tokens': 3}}, 'message_delta')
                yield frame({'type': 'message_stop'}, 'message_stop')
            else:
                yield frame({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if tool else 'stop'}]})
                yield frame({'choices': [], 'usage': usage})
                yield b'data: [DONE]\n\n'
            counts['completed'] += 1
        finally:
            counts['active'] -= 1
            counts['closed'] += 1
    return StreamingResponse(events(), media_type='text/event-stream')
