"""Fixed public mount for the same dashboard and state served on the LAN."""

PUBLIC_PREFIX = '/kvido'
SESSION_COOKIE = 'kvido_session'


def cookie_path(request):
    """Do not derive cookie scope from client supplied forwarding headers."""
    return PUBLIC_PREFIX if request.scope.get('root_path') == PUBLIC_PREFIX else '/'


class PublicPrefixMiddleware:
    """Accept / and /kvido/ without creating a second application instance.

    Install with app.add_middleware(PublicPrefixMiddleware). Apache must retain
    /kvido in the forwarded path. No forwarded-prefix header is trusted.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        path = scope.get('path', '')
        if path == PUBLIC_PREFIX and scope['type'] == 'http':
            query = scope.get('query_string', b'')
            location = PUBLIC_PREFIX.encode() + b'/' + (b'?' + query if query else b'')
            await send({'type': 'http.response.start', 'status': 307,
                        'headers': [(b'location', location), (b'content-length', b'0')]})
            await send({'type': 'http.response.body', 'body': b''})
            return
        if path == PUBLIC_PREFIX or path.startswith(PUBLIC_PREFIX + '/'):
            scope = dict(scope)
            scope['root_path'] = PUBLIC_PREFIX
            # Starlette strips root_path for routing, including StaticFiles.
            # Retain the original path so nested mounts strip the full root.
        await self.app(scope, receive, send)
