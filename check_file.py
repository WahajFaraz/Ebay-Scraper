with open('ebay_scraper.py', 'rb') as f:
    data = f.read()
crlf = data.count(b'\r\n')
lf = data.count(b'\n') - crlf
print('File length:', len(data))
print('CRLF:', crlf)
print('LF:', lf)
print('First 50 bytes:', data[:50])
