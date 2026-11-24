<?php
declare(strict_types=1);

// Run alongside FreshRSS's HTTP health check. No credentials, feed URLs, or
// article bodies are emitted, and PDO is restricted to reads before querying
// the subscription.
//
// The digest feed URL holds a secret token, so match it by prefix instead of
// writing the token into compose:
//   --feed-url-prefix https://rss.example.com/digest/
// --feed-url <exact URL> still works. Exactly one subscription must match.
try {
    $options = getopt('', ['database:', 'feed-url:', 'feed-url-prefix:', 'max-age-seconds:']);
    $database = $options['database'] ?? '';
    $url = $options['feed-url'] ?? '';
    $prefix = $options['feed-url-prefix'] ?? '';
    $maxAge = (int)($options['max-age-seconds'] ?? 7200);
    if (!is_string($database) || !is_file($database) || !is_string($url) || !is_string($prefix)
        || ($url === '') === ($prefix === '') || $maxAge <= 0) {
        throw new RuntimeException('Provide an existing --database, one of --feed-url or --feed-url-prefix, and a positive maximum age');
    }
    $db = new PDO('sqlite:' . $database, null, null, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
    $db->exec('PRAGMA query_only = ON');
    $db->exec('PRAGMA busy_timeout = 5000');
    if ($url !== '') {
        $query = $db->prepare('SELECT lastUpdate, error FROM feed WHERE url = ?');
        $query->execute([$url]);
    } else {
        // substr() rather than LIKE, so "_" and "%" in the prefix are literal.
        $query = $db->prepare('SELECT lastUpdate, error FROM feed WHERE substr(url, 1, ?) = ?');
        $query->execute([strlen($prefix), $prefix]);
    }
    $feeds = $query->fetchAll(PDO::FETCH_ASSOC);
    if (count($feeds) !== 1) {
        throw new RuntimeException(count($feeds) === 0
            ? 'Newsletter subscription is missing or uses the wrong URL'
            : 'More than one subscription matches the newsletter feed URL prefix');
    }
    $feed = $feeds[0];
    if ((int)$feed['error'] !== 0) {
        throw new RuntimeException('FreshRSS reports a newsletter subscription fetch error');
    }
    $age = time() - (int)$feed['lastUpdate'];
    if ($age < -300 || $age > $maxAge) {
        throw new RuntimeException('FreshRSS newsletter subscription has not refreshed within the allowed interval');
    }
    echo "Healthy: FreshRSS newsletter subscription is refreshing\n";
} catch (Throwable $error) {
    // Detailed PDO errors can include local paths; health output needs only the
    // user-facing checks, not connection details.
    echo 'Unhealthy: ', $error instanceof PDOException ? 'Cannot read FreshRSS subscription state' : $error->getMessage(), "\n";
    exit(1);
}
