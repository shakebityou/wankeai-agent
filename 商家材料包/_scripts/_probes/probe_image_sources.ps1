$ErrorActionPreference = 'Continue'
$ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36'
$hdr = @{ 'User-Agent' = $ua }

function Try-Url($name, $url, $check) {
    try {
        $r = Invoke-WebRequest -Uri $url -Headers $hdr -TimeoutSec 20 -UseBasicParsing
        $len = $r.RawContentLength
        Write-Host ("[OK]   {0,-22} HTTP {1}  {2} bytes" -f $name, $r.StatusCode, $len)
        if ($check) {
            $t = $r.Content
            if ($t -is [byte[]]) { $t = [System.Text.Encoding]::UTF8.GetString($t) }
            Write-Host ("       -> " + $check.Invoke($t))
        }
    } catch {
        Write-Host ("[FAIL] {0,-22} {1}" -f $name, $_.Exception.Message)
    }
}

Try-Url 'wikimedia-api' 'https://commons.wikimedia.org/w/api.php?action=query&list=search&srsearch=teddy%20bear%20plush&srnamespace=6&srlimit=5&format=json' {
    param($t) ($t | Select-String -Pattern '"title":"[^"]+' -AllMatches).Matches.Count.ToString() + ' titles'
}

Try-Url 'openverse-api' 'https://api.openverse.org/v1/images/?q=teddy+bear+plush&page_size=5' {
    param($t) if ($t -match '"result_count":(\d+)') { 'result_count=' + $matches[1] } else { 'no field' }
}

Try-Url 'picsum' 'https://picsum.photos/400/400'

Try-Url 'pexels-page' 'https://www.pexels.com/search/teddy%20bear/'

Try-Url 'unsplash-source' 'https://source.unsplash.com/400x400/?teddy-bear'

Try-Url 'bing-images' 'https://www.bing.com/images/search?q=teddy+bear+plush' {
    param($t) if ($t -match 'murl&quot;:&quot;([^&]+)') { 'first murl: ' + $matches[1].Substring(0, [Math]::Min(80, $matches[1].Length)) } else { 'no murl' }
}

Try-Url 'duckduckgo' 'https://duckduckgo.com/?q=teddy+bear&iax=images&ia=images'

Try-Url 'pixabay-api' 'https://pixabay.com/api/?key=demo&q=teddy+bear'
