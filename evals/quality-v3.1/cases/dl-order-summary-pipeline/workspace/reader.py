import csv,io

def read_orders(text):
    return list(csv.DictReader(io.StringIO(text,newline='')))
