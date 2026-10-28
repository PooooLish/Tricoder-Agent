from settings import resolve

DEFAULTS={'port':8000,'debug':False,'label':'app','token':None}

def build_settings(file_values,cli_values):
    return resolve(DEFAULTS,cli_values,file_values)
