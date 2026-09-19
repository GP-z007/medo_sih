"""Provided Udaan artwork for owned chrome and a portable terminal preview."""
import json
import os
import shutil
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / 'images'


def browser_icons():
    from camoufox.pkgman import get_path
    root=Path(get_path('camoufox-bin')).parent/'browser/chrome/icons/default'
    for size in [16,32,48,64,128]:
        source=ASSETS/f'udaan_icon_{size}.png'
        destination=root/f'default{size}.png'
        if source.is_file() and destination.is_file() and source.read_bytes()!=destination.read_bytes():
            backup=destination.with_suffix('.upstream.png')
            if not backup.exists():
                shutil.copy2(destination,backup)
            temporary=destination.with_suffix('.udaan.tmp')
            shutil.copy2(source,temporary)
            os.replace(temporary,destination)


def terminal_title():
    from rich.text import Text
    if os.environ.get('TERM') in {'dumb','unknown'} or os.environ.get('NO_COLOR'):
        return Text(' UDAAN')
    try:
        data=json.loads((ASSETS/'udaan_terminal.json').read_text())
        width,height,pixels=data['width'],data['height'],data['rgba']
        if (width,height,len(pixels))!=(8,6,192):
            raise ValueError('Invalid image preview')
        result=Text()
        for y in range(0,height,2):
            result.append(' ')
            for x in range(width):
                upper=4*(y*width+x)
                lower=upper+4*width
                fg='#'+''.join(f'{v:02x}' for v in pixels[upper:upper+3])
                bg='#'+''.join(f'{v:02x}' for v in pixels[lower:lower+3])
                result.append('▀',style=f'{fg} on {bg}')
            result.append(' UDAAN' if y==2 else '')
            if y<height-2:
                result.append('\n')
        return result
    except (OSError,ValueError,KeyError,TypeError):
        return Text(' UDAAN')
