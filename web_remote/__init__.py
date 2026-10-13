"""
Remote Access: an embedded FastAPI web app (app.py) run on its own
QThread (server.py), giving a browser a Ham Dashboard page and one page
per connected radio, served as a local web app (anything that exposes it beyond this
computer or network is separate software in front of it). See bridge.py for how
Qt's RadioWorker signals get from the GUI thread into this package's
asyncio world without touching a QWidget off the GUI thread.
"""
