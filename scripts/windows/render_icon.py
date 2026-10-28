"""Export the packaged flowing-P artwork as a Windows multi-size icon."""
from pathlib import Path

from PIL import Image


def render_icon(destination: Path):
    import project_watch
    static = Path(project_watch.__file__).parent/'static'
    with Image.open(static/'brand-desktop.png') as master, Image.open(static/'brand-tray.png') as small:
        sizes = (16, 20, 24, 32, 40, 48, 64, 128, 256)
        frames = [(small if n <= 48 else master).resize((n, n), Image.Resampling.LANCZOS) for n in sizes]
        frames[-1].save(destination, format='ICO', sizes=[(n, n) for n in sizes], append_images=frames[:-1])


if __name__ == '__main__':
    import sys
    render_icon(Path(sys.argv[1]))
