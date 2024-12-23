<?php

declare(strict_types=1);

use ReleaseInsights\Performance;

test('Performance Class', function () {
    $obj = new Performance();
    expect($obj->getData())->toBeArray();
    expect($obj->getData())->toHaveCount(3);
});
