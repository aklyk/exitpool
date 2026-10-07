"""Read profile titles through Happ's own context menus (Qt AT-SPI, no OCR).

Adapter for the pinned 4.3.0 (318), one imported subscription, 800x536 window.
Layout/count ambiguities are errors; never silently use the first search result.
"""
import os
from pathlib import Path
import subprocess
import time


class ProfileError(RuntimeError):
    pass


class ProfileMissing(ProfileError):
    """The subscription itself lacks one unambiguous selected profile."""


class StaticScroll:
    """Qt hides/removes the scrollbar when the filtered list fits its viewport."""
    def __init__(self,box):self.box=box;self.currentValue=0
    def queryValue(self):return self


class WheelScroll:
    """Fallback when Qt temporarily omits the accessibility scrollbar after refresh."""
    def __init__(self,ui,box):self.ui=ui;self.box=box;self._value=0
    def queryValue(self):return self
    @property
    def currentValue(self):return self._value
    @currentValue.setter
    def currentValue(self,value):
        if value not in (0,100):raise ProfileError('Unsupported wheel scroll target')
        xd('mousemove',self.ui.x+174,self.ui.y+300,'click','--repeat',300,'--delay',1,4 if value==0 else 5)
        self.ui.settle(.6);self._value=value


def xd(*args):
    return subprocess.check_output(['xdotool', *map(str, args)], timeout=8,
                                   stderr=subprocess.DEVNULL).decode().strip()


class ProfilesUI:
    def __init__(self):
        # docker exec does not inherit the session bus started by our entrypoint.
        address = Path('/run/happ/dbus.address')
        if address.exists(): os.environ['DBUS_SESSION_BUS_ADDRESS'] = address.read_text()
        import pyatspi
        from gi.repository import GLib
        self.spi = pyatspi
        self.context = GLib.MainContext.default()
        self.wid = xd('search', '--onlyvisible', '--name', '^Happ 4[.]3[.]0 [(]318[)]$').splitlines()[0]
        xd('windowsize', self.wid, 800, 536)
        xd('windowmove', self.wid, 240, 182)
        desktop = pyatspi.Registry.getDesktop(0)
        apps = [a for a in desktop if a.name == 'Happ']
        frames = [n for a in apps for n in a if n.name == 'Happ 4.3.0 (318)']
        if len(frames) != 1: raise ProfileError('Happ accessibility window unavailable')
        self.frame = frames[0]
        self.x, self.y, width, height = self.box(self.frame)
        if (width, height) != (800, 536): raise ProfileError('Unsupported Happ window layout')

    def box(self, node):
        if isinstance(node,(StaticScroll,WheelScroll)):return node.box
        try: return tuple(node.queryComponent().getExtents(self.spi.DESKTOP_COORDS))
        except Exception: return (0, 0, 0, 0)

    def click(self, x, y, button=1):
        xd('mousemove', x, y, 'click', button)
        self.settle(.2)

    def settle(self, delay):
        # libatspi's cache is maintained by GLib events. Sleeping alone leaves
        # stale children/menus after filtering or replacing a subscription.
        if not hasattr(self,'context'):
            time.sleep(delay);return
        deadline=time.monotonic()+delay
        while time.monotonic()<deadline:
            for _ in range(100):
                if not self.context.pending():break
                self.context.iteration(False)
            time.sleep(.02)

    def search(self, name=''):
        xd('key', 'Escape')
        self.click(self.x+34, self.y+114)
        self.click(self.x+173, self.y+83)
        xd('key', 'ctrl+a')
        if name:
            # Clipboard preserves emoji, variation selectors and non-Latin names.
            subprocess.run(['xclip', '-selection', 'clipboard'], input=name.encode(), check=True, timeout=8)
            xd('key', 'ctrl+v')
        else: xd('key', 'BackSpace')
        self.settle(.5)

    def children(self):
        panes = [n for n in self.frame if n.getRoleName() == 'filler']
        if len(panes) != 1: raise ProfileError('Unsupported Happ server page')
        return list(panes[0])

    def rows(self):
        return [n for n in self.children() if n.getRoleName() == 'filler'
                and self.box(n)[0] == self.x+92 and self.box(n)[2:] == (310, 48)]

    def geometry(self):
        bars = [n for n in self.children() if n.getRoleName() == 'scroll bar'
                and self.box(n)[0] == self.x+404]
        if len(bars)>1: raise ProfileError('Unsupported Happ subscription list')
        bar = bars[0] if bars else StaticScroll((self.x+404,self.y+112,6,404))
        value = bar.queryValue()
        value.currentValue = 0; self.settle(.4)
        rows = self.rows()
        if not rows: return bar, None, 0, 0
        anchor = min(rows, key=lambda n: self.box(n)[1])
        first_y = self.box(anchor)[1]
        self.first_y = first_y
        # Single subscription header, optionally with a traffic/expiry row.
        if first_y not in (self.y+172, self.y+192):
            raise ProfileError('Unsupported subscription header or multiple groups')
        value.currentValue = 100; self.settle(.4)
        shift = first_y-self.box(anchor)[1]
        if shift < 0: raise ProfileError('Invalid Happ scroll position')
        _, top, _, height = self.box(bar)
        overflow=first_y+len(rows)*48>top+height+1
        if isinstance(bar,StaticScroll) or (not shift and overflow):
            # After replacing a subscription Qt can expose all nine rows but no
            # usable ScrollBar. Native wheel events still scroll its Flickable.
            wheel=WheelScroll(self,(self.x+404,self.y+112,6,404))
            wheel.currentValue=100
            moved=first_y-self.box(anchor)[1]
            if moved>0:bar=wheel;shift=moved
            elif overflow:raise ProfileError('Subscription list has not finished rebuilding')
        if shift:
            _, top, _, height = self.box(bar)
            count, remainder = divmod(top+height-self.box(anchor)[1], 48)
            if remainder or count < len(rows): raise ProfileError('Cannot determine complete profile count')
        else: count = len(rows)
        if not 0 <= count <= 200: raise ProfileError('Subscription exceeds 200 profiles or layout changed')
        return bar, anchor, shift, count

    def position(self, bar, anchor, shift, index):
        _, top, _, height = self.box(bar)
        if isinstance(bar,WheelScroll):
            for _ in range(250):
                center_y=self.box(anchor)[1]+48*index+24
                if top+2<=center_y<=top+height-2:return self.x+174,round(center_y)
                xd('mousemove',self.x+174,self.y+300,'click',4 if center_y<top+2 else 5)
                self.settle(.2)
            raise ProfileError('Cannot scroll the selected profile into view')
        # Do not briefly reset the scroll position here: Qt can animate that reset,
        # making a sampled row coordinate an incorrect origin for the next move.
        first_y = self.first_y
        offset = max(0, min(shift, first_y+48*index+24-(top+height/2)))
        if shift: bar.queryValue().currentValue = 100*offset/shift
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            if abs(self.box(anchor)[1]-(first_y-offset))<=1:break
            self.settle(.05)
        center_y = self.box(anchor)[1]+48*index+24
        if not top+2 <= center_y <= top+height-2:
            raise ProfileError('Profile row is outside the visible list '
                               f'(row={index}, shift={shift}, y={center_y}, viewport={top}:{top+height})')
        return self.x+174, round(center_y)

    def menu_items(self):
        menus = [n for n in self.frame if n.getRoleName() in ('popup menu', 'menu')]
        # Qt exposes the popup as a filler on some builds.
        if not menus:
            menus = [n for n in self.frame if any(c.getRoleName() == 'menu item' for c in n)]
        return [c for m in menus for c in m if c.getRoleName() == 'menu item']

    def title_at(self, x, y):
        self.click(x, y, 3)
        try:
            deadline=time.monotonic()+2
            names=[]
            while time.monotonic()<deadline:
                names=[c.name for c in self.menu_items()]
                if len(names)>=3 and names[-2:]==['Test ping','Remove'] and names[0]:break
                self.settle(.1)
            if len(names) < 3 or names[-2:] != ['Test ping', 'Remove'] or not names[0]:
                raise ProfileError('Cannot read an unambiguous profile title')
            name = names[0]
            if len(name)>240 or any(ord(c)<32 or ord(c)==127 for c in name):
                raise ProfileError('Unsupported control characters in profile name')
            return name
        finally:
            xd('key','Escape');self.settle(.15)

    def catalog(self, query=''):
        self.search(query)
        bar, anchor, shift, count = self.geometry()
        result = []
        try:
            for index in range(count):
                x, y = self.position(bar, anchor, shift, index)
                result.append(self.title_at(x, y))
        finally:
            bar.queryValue().currentValue = 0
        return result

    def select(self, name, exact=True):
        self.search(name)
        bar, anchor, shift, count = self.geometry()
        matches = []
        for index in range(count):
            pos = self.position(bar, anchor, shift, index)
            title = self.title_at(*pos)
            if title == name or not exact: matches.append((index, title))
        if not matches: raise ProfileMissing('Selected profile disappeared or was renamed')
        if len(matches) != 1: raise ProfileMissing('Profile name is ambiguous; no profile selected')
        index, title = matches[0]
        self.click(*self.position(bar, anchor, shift, index))
        return title

    def refresh(self):
        self.search('')
        bar, anchor, shift, count = self.geometry()
        bar.queryValue().currentValue = 0; self.settle(.4)
        # Use the named Update menu action. The inline refresh button can disappear
        # during a state transition, so indexing the header buttons is unsafe.
        buttons = [n for n in self.children() if n.getRoleName() == 'push button'
                   and 18<=self.box(n)[2]<=24 and 18<=self.box(n)[3]<=24
                   and self.x+92<=self.box(n)[0]<=self.x+405
                   and self.y+100<=self.box(n)[1]<=self.y+170]
        buttons.sort(key=lambda n:self.box(n)[0])
        if len(buttons) not in (3,4):raise ProfileError('Subscription menu unavailable')
        box=self.box(buttons[-1]);self.click(box[0]+box[2]//2,box[1]+box[3]//2)
        try:
            updates=[n for n in self.menu_items() if n.name=='Update']
            if len(updates)!=1:raise ProfileError('Subscription Update action unavailable')
            node=updates[0]
            if not node.getState().contains(self.spi.STATE_ENABLED):
                raise ProfileError('Subscription Update action is disabled')
            action=node.queryAction()
            presses=[i for i in range(action.nActions) if action.getName(i)=='Press']
            if len(presses)!=1 or not action.doAction(presses[0]):
                raise ProfileError('Subscription Update action failed')
        finally:xd('key','Escape')


if __name__ == '__main__':
    import json
    print(json.dumps(ProfilesUI().catalog(), ensure_ascii=False))
