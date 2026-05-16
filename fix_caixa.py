lines = open('app/services/scrapers/caixa.py', 'r', encoding='utf-8').readlines()
for i in range(70, len(lines)):
    l = lines[i]
    stripped = l.lstrip()
    if stripped and not l.startswith('    ') and not l.startswith('def ') and not l.startswith('class ') and not l.startswith('#') and not l.startswith('@'):
        lines[i] = '        ' + stripped
open('app/services/scrapers/caixa.py', 'w', encoding='utf-8').writelines(lines)
print('Corrigido!')
