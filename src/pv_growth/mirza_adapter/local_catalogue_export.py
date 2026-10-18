"""Host-side bounded catalogue bridge. Run from the exact deployed image source.

Uses Mirza's installed local connection only on this host. No credentials, card
numbers, customer identifiers or API keys leave PHP. Never mutates Mirza.
"""

import fcntl
import json
import subprocess
import sys

PHP = r"""<?php
require '/var/www/mirza_pro/config.php';

$pdo->exec('SET TRANSACTION READ ONLY');

$pdo->beginTransaction();

try {
$q=$pdo->query("SELECT p.code_product ,
p.name_product ,
p.price_product ,
p.Volume_constraint ,
p.Service_time ,
p.Location FROM product p
WHERE p.agent='f' AND p.one_buy_status='0'
AND (p.hide_panel IS NULL OR p.hide_panel='' OR p.hide_panel='{}')
AND EXISTS (SELECT 1 FROM marzban_panel m
WHERE m.status='active' AND (m.agent='f' OR m.agent='all')
AND (m.name_panel=p.Location OR p.Location='/all'))
ORDER BY CAST(p.price_product AS DECIMAL(18 ,
2)) ,
p.id LIMIT 101");

$rows=$q->fetchAll(PDO::FETCH_ASSOC);

if(count($rows)>100)throw new RuntimeException('bound');

$plans=[];

foreach($rows as $r){
 foreach(['price_product' ,
'Volume_constraint' ,
'Service_time'] as $k){if(!preg_match('/^[0-9]+$/D' ,
(string)$r[$k]))throw new RuntimeException('numeric');
}
 $plans[]=['code'=>$r['code_product'] ,
'name'=>$r['name_product'] ,
'price_toman'=>(int)$r['price_product'] ,
'volume_gb'=>(int)$r['Volume_constraint'] ,
'duration_days'=>(int)$r['Service_time'] ,
'location'=>$r['Location']];

}
$q=$pdo->prepare('SELECT value FROM shopSetting WHERE Namevalue=? LIMIT 1');
$q->execute(['statusdirectpabuy']);
$direct=$q->fetchColumn();

if(!in_array($direct ,
['offdirectbuy' ,
'ondirectbuy'] ,
true))throw new RuntimeException('direct');

$q=$pdo->query("SELECT NamePay ,
ValuePay FROM PaySetting WHERE NamePay IN ('minbalance' ,
'Cartstatus' ,
'Cartstatuspv' ,
'CartDirect') LIMIT 5");

$pay=$q->fetchAll(PDO::FETCH_KEY_PAIR);
$minimum=json_decode($pay['minbalance'] ,
true)['f']??null;

if(!preg_match('/^[0-9]+$/D' ,
(string)$minimum))throw new RuntimeException('minimum');

$payment=['direct_buy'=>$direct==='ondirectbuy' ,
'minimum_wallet_toman'=>(int)$minimum ,
'card_to_support'=>($pay['Cartstatus']??'')==='oncard'&&($pay['Cartstatuspv']??'')==='oncardpv' ,
'support_handle'=>($pay['CartDirect']??'')];

echo 'PUBLIC_CATALOGUE '.json_encode(['source'=>'mirza_local_readonly' ,
'observed_at_utc'=>gmdate('c') ,
'plans'=>$plans ,
'payment'=>$payment] ,
JSON_UNESCAPED_UNICODE).PHP_EOL;

} finally {$pdo->rollBack();
}
"""


SERVICE = """[Unit]
Description=PV GrowthOS local read-only retail catalogue sync
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/pv-growth/shared/retail-catalogue-sync.py
TimeoutStartSec=70
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/run/lock
"""

TIMER = """[Unit]
Description=PV GrowthOS hourly public catalogue refresh

[Timer]
OnCalendar=*-*-* *:03:00
AccuracySec=30s
Persistent=false
Unit=pv-growth-retail-catalogue.service

[Install]
WantedBy=timers.target
"""


def main():
    with open("/run/lock/pv-growth-retail-catalogue.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("catalogue_sync: already_running")
            return 0
        try:
            source = subprocess.run(["/usr/bin/php"], input=PHP, text=True, capture_output=True, timeout=30)
            if source.returncode or len(source.stdout) > 1024 * 1024:
                raise ValueError("source unavailable")
            lines = [s for s in source.stdout.splitlines() if s.startswith("PUBLIC_CATALOGUE ")]
            if len(lines) != 1:
                raise ValueError("source unavailable")
            value = json.loads(lines[0].removeprefix("PUBLIC_CATALOGUE "))
            result = subprocess.run(
                [
                    "/usr/bin/docker",
                    "exec",
                    "-i",
                    "pv-growth-app",
                    "python",
                    "-m",
                    "pv_growth.acquisition.catalogue_import",
                ],
                input=json.dumps(value),
                text=True,
                capture_output=True,
                timeout=30,
            )
            if result.returncode:
                raise ValueError("import unavailable")
            print("catalogue_sync: stored_public_snapshot")
            return 0
        except (ValueError, KeyError, OSError, subprocess.SubprocessError):
            # Do not print PHP stderr, DSNs or credentials. Old snapshot expires.
            print("catalogue_sync: unavailable_existing_snapshot_expires")
            return 1


if __name__ == "__main__":
    sys.exit(main())
