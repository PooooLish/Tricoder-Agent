def summarize(rows):
    result={}
    for row in rows:
        if row['status']=='paid':
            key=row['customer']; result[key]=int(row['amount'])
    return result
