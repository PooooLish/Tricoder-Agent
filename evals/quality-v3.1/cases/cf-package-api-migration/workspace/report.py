from textkit import clean_label

def label_line(text):
    return 'label='+clean_label(text)
