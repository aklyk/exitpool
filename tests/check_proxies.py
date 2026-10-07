#!/usr/bin/env python3
"""Live HTTPS + SOCKS5 UDP tests. Run on the Linux host, not inside Docker."""
import concurrent.futures,json,secrets,socket,struct,subprocess,sys,time

def recv(s,n):
 out=b''
 while len(out)<n:
  b=s.recv(n-len(out))
  if not b:raise ConnectionError('Unexpected EOF')
  out+=b
 return out

def udp_dns(host,port):
 with socket.create_connection((host,port),timeout=8) as tcp, socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
  udp.settimeout(10);udp.bind(('0.0.0.0',0))
  tcp.sendall(b'\x05\x01\x00');assert recv(tcp,2)==b'\x05\x00'
  tcp.sendall(b'\x05\x03\x00\x01'+b'\0'*6)
  head=recv(tcp,4);assert head[:2]==b'\x05\x00',head.hex()
  if head[3]==1:address=socket.inet_ntoa(recv(tcp,4))
  elif head[3]==3:address=recv(tcp,recv(tcp,1)[0]).decode()
  else:raise RuntimeError('Unexpected IPv6 UDP relay')
  relayport=struct.unpack('!H',recv(tcp,2))[0]
  if address=='0.0.0.0':address=host
  ident=secrets.randbelow(65536)
  dns=struct.pack('!6H',ident,0x100,1,0,0,0)+b'\x07example\x03com\0'+struct.pack('!HH',1,1)
  udp.sendto(b'\0\0\0\x01'+socket.inet_aton('1.1.1.1')+struct.pack('!H',53)+dns,(address,relayport))
  packet,_=udp.recvfrom(4096);assert packet[:3]==b'\0\0\0'
  offset={1:10,4:22}.get(packet[3])
  if offset is None:offset=7+packet[4]
  answer=struct.unpack('!6H',packet[offset:offset+12])
  assert answer[0]==ident and answer[1]&0xf==0 and answer[3]>0
  return {'ok':True,'rcode':answer[1]&0xf,'answers':answer[3], 'relay':f'{address}:{relayport}'}

def curl(host,port,url,expected):
 p=subprocess.run(['curl','--silent','--show-error','--fail','--location','--noproxy','',
    '--connect-timeout','6','--max-time','20','--proxy',f'socks5h://{host}:{port}',
    '--write-out','\n%{http_code} %{size_download} %{time_total}',url],capture_output=True,timeout=23)
 if p.returncode:return {'ok':False,'curl_code':p.returncode}
 body,metrics=p.stdout.rsplit(b'\n',1);code,size,seconds=metrics.decode().split()
 r={'ok':int(code)==expected,'http_status':int(code),'bytes':int(size),'seconds':float(seconds)}
 if '/cdn-cgi/trace' in url:
  t=dict(x.split('=',1) for x in body.decode().splitlines() if '=' in x)
  r.update(country=t.get('loc'),ip=t.get('ip'))
 return r

def check(item):
 country,host,port=item
 out={'country':country,'address':f'{host}:{port}','at':time.time()}
 out['trace']=curl(host,port,'https://www.cloudflare.com/cdn-cgi/trace',200)
 out['trace']['ok']=out['trace']['ok'] and (not country or out['trace'].get('country')==country)
 out['google']=curl(host,port,'https://www.gstatic.com/generate_204',204)
 out['meduza']=curl(host,port,'https://meduza.io/',200)
 try:out['udp_dns']=udp_dns(host,port)
 except Exception as e:out['udp_dns']={'ok':False,'error':type(e).__name__+': '+str(e)}
 return out
if __name__=='__main__':
 import argparse
 from pathlib import Path
 source=Path(__file__).resolve().parents[1]
 sys.path.insert(0,str(source/'lib'))
 from configuration import load_instances, relay_address
 installed=Path('/opt/exitpool/instances.json')
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--instances',type=Path,default=installed)
 args=parser.parse_args()
 config=load_instances(args.instances)
 items=[(c['country'],relay_address(c),c['port']) for c in config.values()]
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(check,items))
 print(json.dumps(results,ensure_ascii=False,indent=2))
 sys.exit(0 if all(v['ok'] for r in results for k,v in r.items() if isinstance(v,dict)) else 1)
