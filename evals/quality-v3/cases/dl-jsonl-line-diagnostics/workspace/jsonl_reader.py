import json

def parse_lines(text):
    records=[]; errors=[]
    for number,line in enumerate((l for l in text.splitlines() if l.strip()),1):
        if not line.strip(): continue
        try:
            value=json.loads(line)
            if not isinstance(value,dict): raise ValueError('object required')
        except ValueError: errors.append(number)
        else: records.append(value)
    return records,errors
