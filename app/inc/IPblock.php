<?php

declare(strict_types=1);

use ReleaseInsights\Utils;

// Is that a known suspicious IP?
$ips = [];
$target = CACHE_PATH . 'blockedIPs.json.cache';
if (file_exists($target)) {
    $ips = json_decode(file_get_contents($target));
}

$client_ip = Utils::getIP();

// Log suspicious IPs, $url object is defined in init.php
if (Utils::inString($url->path, ['wp-admin', 'wp-content'])) {
    if (! in_array($client_ip, $ips) ) {
        $ips[] = $client_ip;
        file_put_contents($target, json_encode($ips));
        Utils::dump("Suspicious $client_ip added to blockedIPs.json.cache");
    }
}

// Block suspicious IPs
 if (in_array($client_ip, $ips)) {
    http_response_code(403);
    die('IP blocked.');
}