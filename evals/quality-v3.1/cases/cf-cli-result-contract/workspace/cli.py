import sys
from arithmetic import add

def main(argv):
    try:
        if len(argv)!=2: raise ValueError('two integers required')
        a,b=map(int,argv)
    except ValueError:
        print('two integers required',file=sys.stderr)
        return 2
    print('result='+str(add(a,b)))
    return 0

if __name__=='__main__':
    raise SystemExit(main(sys.argv[1:]))
