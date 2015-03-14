#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Test the in-memory and disk cache for pySlip tiles.

Requires a wxPython application to be created before use.
If we can create a bitmap without wxPython, we could remove this dependency.
"""

import tile_cache

import os
import wx
import unittest
import shutil
from wx.lib.embeddedimage import PyEmbeddedImage


# if we don't have log.py, don't crash
try:
    import log
    #log = log.Log('pyslip.log', log.Log.DEBUG)
    log = log.Log('pyslip.log', log.Log.INFO)
except ImportError:
    def log(*args, **kwargs):
        pass


TilesDir = './tiles_test'

DefaultAppSize = (512, 512)
DemoName = 'Tiles Cache Test'
DemoVersion = '0.1'

def getImage():
    """Generate image from embedded data.
    
    We used to have a separate image file for this, but this is better.
    """

    return PyEmbeddedImage(
      "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"
      "ACAAAAQAgAAAEAAAAoD/JPr6+i5duhSPxwkhXq+3s7Mz9T7nHAHA7zLOOWNsfHw8LS3N6/W2"
      "traGQqFkMtnW1kYIoZT+PwhgJaMv2RPAA5v9lNL29vbi4mKbzfary5dVVXW73Y2NjTt27Ghp"
      "afkyWyCEiIuLi36/v6CgQJKk1R8zxmKxmCzLfX19t27dKi0txaDDo4NSurS01NXVtXv37p+/"
      "/fbwxIR3eHhofLyyuFhRlKtXrzY0NHzhFs6fPy84HI7Ozs7XXnvtc7/0PT09x44dI4QMDw/f"
      "vHnzC48DOETAA+b1enfu3EkI+f53v3tvbESipMyd53K5Lly4sHXr1i+zhTfffFN84YUXFEX5"
      "8MMPTSZTRkaG0Wi8du3a5cuXz507d/v27YmJCZfL5fF4uru7x8bGamtrZ2ZmmpubI5FIfn7+"
      "kSNHenp6Wltbq6urrVYrdgk8GPF4/MyZM3fuDJ15t6W7u/vK9Qvf+t42h9N25eKVf3z/F+FE"
      "UIvRjo6Ourq6EydOlJSUNDc3V1VVtbW1Wa3Wo0ePbty4sbe3t7m5eXR0VBwYGOjt7X355Zfb"
      "2tomJiacTufhw4effvppWZYDgcCzzz4bDodbW1t3797d0tJSVVV18ODBioqK06dPr1279uTJ"
      "k8vLy2az2e/3b9iwATsGHlgA165dU6XFdd8oN1jFkvICQohOJzvzHO4CV7ZT8fR7v1G/sbi4"
      "+JVXXlFV9d13301PTz937tzzzz9/6dKlCxcutLe379u3r6OjQ/rJT35itVr1er3dbj906FBP"
      "T09jY+O2bdvS0tI8Hs+mTZvee++9pqamvXv3njp16uOPP7ZarQcPHhwbG/N6vZzzQ4cOffDB"
      "B6lFKIAHw2w2r6mu8EdGBSpYLGZCCCecEGI2m1JfULLtlRVrCCF5eXnt7e2ZmZnt7e319fWy"
      "LLvd7paWlgMHDqxbty4ajQp2u12v1xNCCgsL6+vrBwcH9+/fn7pEiMfjnPN4PG6xWAghdrtd"
      "UZRwOHzixInR0dHNmzcLgmCxWKxW68LCAvYKPEiyTkrN+1AovBBc4pyHQ5GVT1MzkxBSXV1t"
      "MBj2798/MjLS1NR06tSpGzduvPrqq++8887169c1TRMPHDiw8tcuXrxYV1e3a9cuQojRaMzK"
      "yiosLHQ4HJWVlXa7PS8vr7a2dvv27X6//7nnnisvL1cUpbS01OVypaen5+XlYa/AA+PzzyxG"
      "ZhljgdngrG/eZrd03+h35jkkUSSEJCLClsZtqZ/12trazZs3W63WLVu2WK3WZ555prKysqGh"
      "wW63b9++nV6+fDl1y2BmZubFF188ffq02WxevbaT+nTlxb+zCvQlbz0AfH3hUOStljcys9NW"
      "3kkkVFnWEULueEdf+qNXFEVJvc8Y0zRNEATG2GcHEFlOvZBWZq3RaHzppZfMZvPqqfybL34b"
      "zH54kGLxqBbW9dztL6rMt9ushBBZ1jHOxu/c21j7pKIoqWmsadr7Fy4tLCwNDA5LokAoIYRE"
      "o/G//ovD9wNY2aLNZmtqasJUhsfCxMSEyWR6svDJjFzXP7W3BcNzAiGMc5kahRphZRpzzjMy"
      "MvLcuZ7BO4IoEEIooaIormxHwlDC46impqa/u3vr7t02my3LkX28+Y21dZWJZCK++Ft/vilJ"
      "JfGvbtpKPV3XNKa580uznbiKhcfGyMiI3W632WyEkHA4LEnSQnDJ4xkrL6t44oknVi8HpQ4F"
      "92c/4TpZcGVaen59jXBCCJH8vvFkMpmhZGFM4XHBOb/d27s4P5/6z/7+fmeuY2Y6IOt1Fpsl"
      "qar/fP59URQJJYmENhOKllVWpG4UUEJFge/5vW+Nj3iWI0uU0v8LFBxSmlO0UKUAAAAASUVO"
      "RK5CYII=").GetImage()


class AppFrame(wx.Frame):

    def __init__(self):
        wx.Frame.__init__(self, None, size=DefaultAppSize,
                          title='%s %s' % (DemoName, DemoVersion))
        self.SetMinSize(DefaultAppSize)
        self.panel = wx.Panel(self, wx.ID_ANY)
        self.panel.SetBackgroundColour(wx.WHITE)
        self.panel.ClearBackground()
        self.Bind(wx.EVT_CLOSE, self.onClose)

        unittest.main()

    def onClose(self, event):
        self.Destroy()

class TestCache(unittest.TestCase):
    def setUp(self):
        log.info('setUp')
        self.tile_image = getImage()
        self.tile_bitmap = self.tile_image.ConvertToBitmap()
        # we get error here if TilesDir exists, and that is perhaps
        # the desired behaviour
        os.makedirs(TilesDir)

    def tearDown(self):
        log.info('tearDown')
        shutil.rmtree(TilesDir)

    def testSimple(self):
        """Cache & retrieve 1 tile from a simple cache."""

        log.info('testSimple')
        cache = tile_cache.TileCache(TilesDir, [0])
        cache.CacheTile(self.tile_image, self.tile_bitmap, 0, 0, 0)
        tile = cache.GetTile(0, 0, 0)
        self.failUnless(tile == self.tile_bitmap)

    def testBigger(self):
        """Cache & retrieve 1 tile to many levels"""

        log.info('testBigger')
        levels = [0, 1, 2, 3, 4, 5]
        cache = tile_cache.TileCache(TilesDir, levels)
        for l in levels:
            log.debug('testBigger: 1 %d' % l)
            cache.CacheTile(self.tile_image, self.tile_bitmap, l, l, l)
        for l in levels:
            log.debug('testBigger: 2 %d' % l)
            tile = cache.GetTile(l, l, l)
            self.failUnless(tile == self.tile_bitmap)

    def testLRU(self):
        """Cache & retrieve lots of tiles from many levels.

        Tile comparison actually compares id() values.
        These should be different only when tile is fetched from disk.
        """

        log.info('testLRU')
        orig_width = self.tile_bitmap.GetWidth()
        orig_height = self.tile_bitmap.GetHeight()
        orig_depth = self.tile_bitmap.GetDepth()
        num_tiles = 1000    # number of tiles in each level
        levels = [0, 1, 2, 3]
        # cache with max in-memory number of tiles less then level max
        cache = tile_cache.TileCache(TilesDir, levels, max_in_mem=num_tiles/2)

        # should create unique images...
        for l in levels:
            for n in range(num_tiles):
                log.debug('testLRU: 1 level=%d, tile#=%d' % (l,n))
                cache.CacheTile(self.tile_image, self.tile_bitmap, l, n, n)

        # should test for the identical image...
        for l in levels:
            for n in range(num_tiles):
                log.debug('testLRU: 2 level=%d, tile#=%d' % (l,n))
                tile = cache.GetTile(l, n, n)
                if (tile.GetWidth() != orig_width or
                    tile.GetHeight() != orig_height or
                    tile.GetDepth() != orig_depth):
                    msg = ('level %d, tile %d: '
                           'retrieved tile != self.tile_bitmap'
                           % (l, n))
                    self.fail(msg)
                if (tile != self.tile_bitmap and    # compares id() values
                    cache._get_cache_size(l) <
                        cache.max_in_mem):
                    msg = ('level %d, tile %d: '
                           'retrieved tile != self.tile_bitmap '
                           'and cache not full (%d,%d)'
                           % (l, n, cache._get_cache_size(l),
                              cache.max_in_mem))
                    self.fail(msg)

app = wx.App()
app_frame = AppFrame()
app_frame.Show()
app.MainLoop()