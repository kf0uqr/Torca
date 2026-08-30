"""
Remote Access: an embedded FastAPI web app (app.py) run on its own
QThread (server.py), giving a browser a Ham Dashboard page and one page
per connected radio -- reachable locally and, via cloudflare_tunnel.py,
over the internet through a Cloudflare Tunnel. See bridge.py for how
Qt's RadioWorker signals get from the GUI thread into this package's
asyncio world without touching a QWidget off the GUI thread.
"""
