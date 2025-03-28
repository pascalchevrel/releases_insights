<?php

declare(strict_types=1);

namespace ReleaseInsights;

class Performance
{
    /**
     * Utility function to return the memory used by a script
     * and the time needed to compute the data.
     *
     * @return array<int, float|int<1, max>> [Memory peak in bytes, Memory peak in MB, Computation time]
     */
    public static function getData(): array
    {
        $memory_peak_B = memory_get_peak_usage(true);
        $memory_peak_MB = round($memory_peak_B / (1024 * 1024), 2);
        $computation_time = round(microtime(true) - $_SERVER['REQUEST_TIME_FLOAT'], 4);

        return [$memory_peak_B, $memory_peak_MB, $computation_time];
    }

    /**
     * Utility function to log to stderr the memory used by a script
     * and the time needed to generate the page.
     */
    // @codeCoverageIgnoreStart
    public static function log(): void
    {
        [$memory_peak_B, $memory_peak_MB, $computation_time] = self::getData();
        Debug::dump(
            "Memory peak: {$memory_peak_B} ({$memory_peak_MB}MB)",
            "Elapsed time (s): {$computation_time}",
            debug_backtrace()[0]['file'] . ': ' . (string) debug_backtrace()[0]['line']
        );
    }
    // @codeCoverageIgnoreEnd

    /**
     * Utility function to log the memory used by a script
     * and the time needed to generate the page as an HTTP header.
     */
    // @codeCoverageIgnoreStart
    public static function HTTPHeader(): void
    {
        [$memory_peak_B, $memory_peak_MB, $computation_time] = self::getData();
        header("App-perf: Memory: {$memory_peak_B} ({$memory_peak_MB}MB); Time: {$computation_time}s");
    }
    // @codeCoverageIgnoreEnd
}
