"""Safety properties of exact selection; actual Qt traversal is checked live."""
import sys
from pathlib import Path
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'happ'))
from runtime.ui_profiles import ProfilesUI, ProfileError, StaticScroll


class FakeUI(ProfilesUI):
    def __init__(self,names):self.names=names;self.clicked=[]
    def search(self,name=''):self.search_name=name
    def geometry(self):return None,None,0,len(self.names)
    def position(self,bar,anchor,shift,index):return index,0
    def title_at(self,x,y):return self.names[x]
    def click(self,x,y,button=1):self.clicked.append(x)


class SelectionTests(unittest.TestCase):
    def test_single_filtered_row_without_scrollbar(self):
        ui=ProfilesUI.__new__(ProfilesUI);ui.x=240;ui.y=182
        node=object()
        ui.children=lambda:[]
        ui.rows=lambda:[node]
        original=ui.box
        ui.box=lambda n: (332,354,310,48) if n is node else original(n)
        with mock.patch('runtime.ui_profiles.time.sleep'),mock.patch('runtime.ui_profiles.xd'):
            bar,anchor,shift,count=ui.geometry()
            self.assertEqual(ui.position(bar,anchor,shift,0),(414,378))
        self.assertIsInstance(bar,StaticScroll)
        self.assertEqual((shift,count),(0,1))

    def test_exact_match_does_not_select_first_search_hit(self):
        ui=FakeUI(['Germany backup','Germany ⚡️','Germany'])
        self.assertEqual(ui.select('Germany'),'Germany')
        self.assertEqual(ui.clicked,[2])

    def test_duplicate_exact_names_refuse_any_selection(self):
        ui=FakeUI(['Germany','Germany'])
        with self.assertRaises(ProfileError):ui.select('Germany')
        self.assertEqual(ui.clicked,[])

    def test_missing_or_renamed_profile_refuses_fallback(self):
        for names in ([],['Germany NEW'],['Sweden']):
            ui=FakeUI(names)
            with self.assertRaises(ProfileError):ui.select('Germany')
            self.assertEqual(ui.clicked,[])

    def test_legacy_partial_query_must_also_be_unique(self):
        ui=FakeUI(['Germany','Germany Backup'])
        with self.assertRaises(ProfileError):ui.select('Germ',exact=False)
        self.assertEqual(ui.clicked,[])


if __name__=='__main__':unittest.main()
