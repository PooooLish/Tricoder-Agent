def allowed(args: list[str]) -> bool:
    if not args or any(token in {'&&', '||', ';', '|'} for token in args):
        return False
    return args[:3] == ['python', '-m', 'unittest']
