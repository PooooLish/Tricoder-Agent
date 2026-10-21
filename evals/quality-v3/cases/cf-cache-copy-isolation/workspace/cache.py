from copy import deepcopy

class RecordCache:
    def __init__(self): self._data={}
    def put(self,key,value): self._data[key]=value
    def get(self,key): return self._data[key]
