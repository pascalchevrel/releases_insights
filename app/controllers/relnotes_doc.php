<?php

declare(strict_types=1);

// Link to the current Release Notes draft doc. Update that link every cycle.
$doc = 'https://docs.google.com/document/d/1i_dQ993vGYS___EJtTd72bWidi8TNQz_RBvNfs4ow78/edit?usp=sharing';

header("Location: $doc", true, 302);
exit;
