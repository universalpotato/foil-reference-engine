"""Hand-written card definitions (from Oracle text) for cards the template compiler can't
express. Each entry cites the Oracle text it implements. Keyed by face name."""

_REGISTRY = {}


def card(name):
    def deco(fn):
        _REGISTRY[name] = fn
        return fn
    return deco


def get(face):
    fn = _REGISTRY.get(face.name)
    if fn is None:
        return None
    return fn(face)


from . import manual  # noqa: E402,F401  (registers definitions)
