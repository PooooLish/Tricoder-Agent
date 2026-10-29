def resolve(defaults,file_values,cli_values):
    return {**cli_values,**file_values,**defaults}
