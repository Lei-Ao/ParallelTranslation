"""Deterministic multimodal HTTP fixture: no external API or local model."""
import json
import httpx


def client(seen=None):
    def handler(request):
        body = json.loads(request.content)
        messages = body.get('messages', [])
        images = [part for message in messages if isinstance(message.get('content'), list)
                  for part in message['content'] if part.get('type') == 'image_url']
        assert images and all(p['image_url']['url'].startswith('data:image/png;base64,') for p in images)
        if seen is not None:
            seen.append({'model': body.get('model'), 'images': len(images), 'url': str(request.url)})
        result = {'status': 'passed', 'issues': []} if len(images) == 2 else {'regions': []}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


def install(test):
    from unittest.mock import patch
    import pdf_model
    transport = client()
    test.addCleanup(transport.close)
    replacement = patch.object(pdf_model, '_CLIENT', transport)
    replacement.start()
    test.addCleanup(replacement.stop)
