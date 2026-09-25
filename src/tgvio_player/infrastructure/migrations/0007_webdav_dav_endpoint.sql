UPDATE player_storage_settings
SET endpoint_url = 'https://file.722225.xyz/dav'
WHERE singleton = 1
  AND endpoint_url = 'https://file.722225.xyz';
