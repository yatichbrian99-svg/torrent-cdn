"""Gunicorn settings for the production image.

One job: stop advertising the server software and its version.

Gunicorn writes ``Server: <gunicorn.SERVER>`` on every response
(``gunicorn/http/wsgi.py``) and uvicorn's ASGI layer writes its own ``server``
header; between them every response named the exact runtime and gunicorn release.
ZAP's 10036 fired on it five times in the authenticated pass. The app sets the
ASGI-side header (``SERVER_HEADER`` in ``app/main.py``); this patches the module
constant gunicorn copied into its response class at import time, which is the only
way to change gunicorn's own header short of vendoring it.

Loaded with ``gunicorn --config gunicorn_conf.py`` (see ``Dockerfile``). It is not
a config class, only a patch, so gunicorn's defaults still apply.
"""

from __future__ import annotations

from gunicorn.http import wsgi

#: What every response now says. No version, no product family.
SERVER_NAME = "torrentcdn"

#: `Response.__init__` does `self.version = SERVER`, so the module-local name is
#: what matters; setting `gunicorn.SERVER` alone would not reach it.
wsgi.SERVER = SERVER_NAME
#: The WSGI environ value (`SERVER_SOFTWARE`) for completeness — some error
#: pages and third-party middlewares read it.
wsgi.SERVER_SOFTWARE = SERVER_NAME
