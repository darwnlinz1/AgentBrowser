import asyncio
import websockets
import json

async def test():
    for origin in ['http://127.0.0.1:9222', 'http://localhost:9222', 'chrome-devtools://devtools', 'https://chrome-devtools-frontend.appspot.com']:
        for path in ['', '/devtools/browser', '/json']:
            url = f'ws://127.0.0.1:9222{path}'
            try:
                extra_headers = {'Origin': origin}
                ws = await websockets.connect(url, extra_headers=extra_headers, ping_interval=None, open_timeout=2)
                print(f'SUCCESS with origin={origin} on {url}!')
                await ws.send(json.dumps({'id': 1, 'method': 'Target.getTargets'}))
                res = await ws.recv()
                print('Result:', res[:200])
                await ws.close()
                return
            except Exception as e:
                pass
    print('All origin combinations tried')

if __name__ == '__main__':
    asyncio.run(test())
