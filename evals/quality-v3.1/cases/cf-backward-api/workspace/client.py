from converter import slugify

def route(title: str) -> str:
    return '/docs/' + slugify(title)
