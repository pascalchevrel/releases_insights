<?php

declare(strict_types=1);

use ReleaseInsights\{Model, Template};

[$data, $bug_details, $uplift_counter, $stats] = (new Model('beta'))->get();

(new Template(
    'beta.html.twig',
    [
        'page_title'   => 'Betas this cycle',
        'css_page_id'  => 'beta',
        'betas_data'   => $data,
        'bug_list'     => $bug_details,
        'uplift_count' => $uplift_counter,
        'stats'        => $stats,
    ]
))->render();