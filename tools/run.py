"""Launch a persistent random-port LAN server or localhost preview."""
import argparse
import ipaddress
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from datetime import datetime,timedelta,timezone

root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root));os.chdir(root)
parser=argparse.ArgumentParser()
parser.add_argument('--host',default='127.0.0.1')
parser.add_argument('--port',type=int)
parser.add_argument('--https',action='store_true')
parser.add_argument('--ip',help='LAN IP for the HTTPS certificate')
args=parser.parse_args()
data=Path(os.environ.get('ROVER_DATA',root/'data'));data.mkdir(exist_ok=True)
port_file=data/'port.json'
if args.port:port=args.port
elif port_file.exists():port=json.loads(port_file.read_text())['port']
else:
    for _ in range(100):
        port=20000+secrets.randbelow(19999)
        with socket.socket() as s:
            try:s.bind((args.host,port));break
            except OSError:continue
    else:raise RuntimeError('No free port')
    port_file.write_text(json.dumps({'port':port}))
options={}
if args.https:
    from cryptography import x509
    from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    if not args.ip:parser.error('--https requires --ip with the LAN IP')
    ip=ipaddress.ip_address(args.ip)
    public_host=os.environ.get('ROVER_PUBLIC_HOST','').strip()
    ca_path=data/'kvido-local-ca.crt';ca_key_path=data/'ca-key.pem';cert_path=data/'server.crt';key_path=data/'server-key.pem'
    now=datetime.now(timezone.utc)
    def save_key(path,key):
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));path.chmod(0o600)
    if not ca_path.exists():
        ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Kvido local development CA')])
        ca=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).add_extension(x509.KeyUsage(False,False,False,False,False,True,True,False,False),critical=True).sign(ca_key,hashes.SHA256()))
        ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM));save_key(ca_key_path,ca_key)
    else:
        ca=x509.load_pem_x509_certificate(ca_path.read_bytes());ca_key=serialization.load_pem_private_key(ca_key_path.read_bytes(),password=None)
    # Issue a new server certificate when the IP changes or validity is short.
    issue=True
    if cert_path.exists() and key_path.exists():
        existing=x509.load_pem_x509_certificate(cert_path.read_bytes())
        issue=ip not in existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress) or existing.not_valid_after_utc<now+timedelta(days=7)
        if public_host and public_host not in existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName):issue=True
    if issue:
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,str(ip))])
        san=[x509.IPAddress(ip),x509.DNSName('localhost')]
        if public_host:san.append(x509.DNSName(public_host))
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=90)).add_extension(x509.SubjectAlternativeName(san),critical=False).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).sign(ca_key,hashes.SHA256()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM));save_key(key_path,key)
    options={'ssl_certfile':str(cert_path),'ssl_keyfile':str(key_path)}
url=f'{"https" if args.https else "http"}://{args.ip or args.host}:{port}'
(data/'Kvido-garaz.url').write_text('[InternetShortcut]\nURL='+url+'/\n',encoding='utf-8')
print('Kvido: '+url,flush=True)
print('Access key: '+str(data/'access.key'),flush=True)
if args.https:print('Install/trust the public CA on the phone: '+str(ca_path),flush=True)
import uvicorn
uvicorn.run('server.app:app',host=args.host,port=port,proxy_headers=False,
            ws='websockets',ws_max_size=160*1024,ws_max_queue=8,
            ws_per_message_deflate=False,timeout_graceful_shutdown=5,**options)
