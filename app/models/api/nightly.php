<?php

declare(strict_types=1);

use Cache\Cache;
use ReleaseInsights\{Json, URL, Utils};

$date = Utils::getDate();

$options = [
    'http' => [
        'method'  => 'POST',
        'header'  =>   "Content-Type: application/json\r\n"
                     . "User-Agent: WhatTrainIsItNow/1.0\r\n"
                     . "Referer: https://whattrainisitnow.com",
        // Don't turn an HTTP error (406, 429…) into a `false` return value and
        // don't let a stalled Buildhub hang the whole page.
        'ignore_errors' => true,
        'timeout'       => 15,
        'content' => '{
      "_source": ["build.id", "source.revision", "target.version"],
      "query": {
        "bool": {
          "must": [
            { "match": { "source.product": "firefox" }},
            { "match": { "target.locale": "en-US" }},
            { "match": { "target.channel": "nightly" }},
            { "regexp": { "build.id": "' . $date . '.*" }}
          ]
        }
      }
    }',
    ],
];

// The date in the string varies so we create a unique file name in cache
$cache_id = $options['http']['content'];

// If we can't retrieve cached data, we create and cache it.
// We cache because we want to avoid http request latency
if (! $data = Cache::getKey($cache_id, 900)) {
    $data = @file_get_contents(
        URL::BuildHub->value,
        false,
        stream_context_create($options)
    );

    // Buildhub is unreachable, rate-limited or answered with something that
    // isn't JSON. Don't cache and let the caller fall back to archive.m.o.
    if (! is_string($data) || ! json_validate($data)) {
        return [];
    }

    $data = array_column(Json::toArray($data)['hits']['hits'] ?? [], '_source');

    // No data returned, bug or incorrect date, don't cache.
    if (empty($data)) {
        return [];
    }

    // Build a [buildid => [revision, version]] array
    $filtered = [];
    foreach ($data as $value) {
        // Skip incomplete records instead of failing on a missing key
        if (! isset($value['build']['id'], $value['source']['revision'], $value['target']['version'])) {
            continue;
        }

        $filtered[$value['build']['id']] = ['revision' => $value['source']['revision'], 'version' => $value['target']['version']];
    }

    // We sort the array by key because we want the builds to be in chronological order
    ksort($filtered);

    $data = $filtered;

    // We don't cache today because we may miss the second nightly build
    if ($date !== date('Ymd')) {
        Cache::setKey($cache_id, $data);
    }
}

return $data;
