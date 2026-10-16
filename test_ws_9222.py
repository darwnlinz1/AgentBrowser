import asyncio
import websockets
import json

async def test():
    for path in ['/devtools/browser', '', '/devtools/page', '/ws']:
        url = f'ws://127.0.0.1:9222{path}'
        try:
            ws = await websockets.connect(url, ping_interval=None)
            print(f'Successfully connected to {url}')
            await ws.send(json.dumps({'id': 1, 'method': 'Target.getTargets'}))
            res = await ws.recv()
            print('Result:', res[:200])
            await ws.close()
            return
        except Exception as e:
            print(f'Failed {url}: {e}')

if __name__ == '__main__':
    asyncio.run(test())
