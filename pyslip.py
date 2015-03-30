#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
A 'slippy map' widget for wxPython.

So why is this widget called 'pySlip'?

Well, in the OpenStreetMap world[1], a 'slippy map' is a browser map view
served by a tile server that can be panned and zoomed in the same way as
popularised by Google maps.  Such a map feels 'slippery', I guess.

Rather than 'slippy' I went for the slightly more formal 'pySlip' since the
thing is written in Python and therefore must have the obligatory 'py' prefix.

Even though this was originally written for a geographical application, the
underlying system only assumes a cartesian 2D coordinate system.  So pySlip
could be used to present a game map, 2D CAD view, or whatever.  The major
difficulty for most uses is to generate the map tiles.

[1] http://wiki.openstreetmap.org/index.php/Slippy_Map
"""


import os
import sys
import glob
import json
try:
    import cPickle as pickle
except ImportError:
    import pickle
from PIL import Image
import wx

# if we don't have log.py, don't crash
try:
    import log
    log = log.Log('pyslip.log', log.Log.DEBUG)
except ImportError:
    def log(*args, **kwargs):
        pass


__version__ = '3.0'

__all__ = ['PySlip']

# type of SELECT events
EventPointSelect = 0
EventBoxSelect = 1
EventRightPointSelect = 2
EventRightBoxSelect = 3


######
# utility routines.
######

def point_inside_polygon(x, y, poly):
    """Decide if point is inside polygon.

    x     x coord of point in question
    y     y coord of point in question
    poly  polygon in form [(x1,y1), (x2,y2), ...]

    Returns True if point is properly inside polygon.
    May return True or False if point on edge of polygon.

    Slightly modified version of the 'published' algorithm found on the 'net.
    Instead of indexing into the poly, create a new poly that 'wraps around'.
    Even with the extra code, it runs in 2/3 the time.
    """

    l_poly = list(poly)
    new_poly = l_poly[:]
    new_poly.append(l_poly[0])

    inside = False

    (p1x, p1y) = new_poly[0]

    for (p2x, p2y) in new_poly:
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y-p1y)*(p2x-p1x)/(p2y-p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        (p1x, p1y) = (p2x, p2y)

    return inside


######
# Base class for the widget canvas - buffered and flicker-free.
######

class _BufferedCanvas(wx.Panel):
    """Implements a buffered, flicker-free canvas widget.

    This class is based on:
        http://wiki.wxpython.org/index.cgi/BufferedCanvas
    """

    # The backing buffer
    buffer = None

    def __init__(self, parent, id=wx.ID_ANY, pos=wx.DefaultPosition,
                 size=wx.DefaultSize, style=wx.NO_FULL_REPAINT_ON_RESIZE):
        """Initialize the canvas.

        parent  reference to 'parent' widget
        id      the unique widget ID (NB: shadows builtin 'id')
        pos     canvas position
        size    canvas size
        style   wxPython style
        """

        wx.Panel.__init__(self, parent, id, pos, size, style)

        # Bind events
        self.Bind(wx.EVT_PAINT, self.OnPaint)
        self.Bind(wx.EVT_SIZE, self.OnSize)

        # Disable background erasing (flicker-licious)
        def disable_event(*args, **kwargs):
            pass            # the sauce, please
        self.Bind(wx.EVT_ERASE_BACKGROUND, disable_event)

        # set callback upon onSize event
        self.onSizeCallback = None

    def Draw(self, dc):
        """Stub: called when the canvas needs to be re-drawn."""

        pass

    def Update(self):
        """Causes the canvas to be updated."""

        dc = wx.BufferedDC(wx.ClientDC(self), self.buffer)
        dc.BeginDrawing()
        dc.Clear()
        log('Update: before self.Draw(dc)')
        self.Draw(dc)
        log('Update: after self.Draw(dc)')
        dc.EndDrawing()

    def OnPaint(self, event):
        """Paint the canvas to the screen."""

        # Blit the front buffer to the screen
        wx.BufferedPaintDC(self, self.buffer)

    def OnSize(self, event=None):
        """Create a new off-screen buffer to hold drawn data."""

        (width, height) = self.GetClientSizeTuple()
        if width == 0:
            width = 1
        if height == 0:
            height = 1
        self.buffer = wx.EmptyBitmap(width, height)

        self.view_width = width
        self.view_height = height

        # call onSize callback, if registered
        if self.onSizeCallback:
            self.onSizeCallback()

        # Now update the screen
        self.Update()

######
# A layer class - encapsulates all layer data.
######

class _Layer(object):
    """A Layer object."""

    DefaultDelta = 5        # default selection delta

    def __init__(self, id=0, painter=None, data=None, map_rel=True,
                 visible=False, show_levels=None, selectable=False,
                 name="<no name given>", type=None):
        """Initialise the Layer object.

        id           unique layer ID
        painter      render function
        data         the layer data
        map_rel      True if layer is map-relative, else layer-relative
        visible      layer visibility
        show_levels  list of levels at which to auto-show the level
        selectable   True if select operates on this layer, Else False
        name         the name of the layer (for debug)
        type         a layer 'type' flag
        """

        self.painter = painter          # routine to draw layer
        self.data = data                # data that defined the layer
        self.map_rel = map_rel          # True if layer is map relative
        self.visible = visible          # True if layer visible
        self.show_levels = show_levels  # None or list of levels to auto-show
        self.selectable = selectable    # True if we can select on this layer
        self.delta = self.DefaultDelta  # minimum distance for selection
        self.name = name                # name of this layer
        self.type = type                # type of layer
        self.id = id                    # ID of this layer

    def __str__(self):
        return ('<pyslip Layer: id=%d, name=%s, map_rel=%s, visible=%s'
                % (self.id, self.name, str(self.map_rel), str(self.visible)))

###############################################################################
# A Resource class that abstracts loading/storing resources from/to disk.
###############################################################################

class Resource(object):
    """A class to allow the loading of layer data to/from disk as a resource.

    An instance of Resource has the following attributes/methods:
        .layers      a dictionary of named Layer objects
        .AddLayer()  add a layer to the resource
        .GetLayer()  get a layer resource by name and type
        .Read()      read a resource from disk
        .Write()     write resource to disk
    """

    def __init__(self, fname=None):
        """Initialise a Resource object, optionally loading data from disk.

        fname  path to a resource file to read
        """

        # set default attributes
        self.layers = {}
        self.filename = fname
        if fname:
            self.Read(fname)

    def Read(self, fname):
        """Read a resource from disk.

        fname  path to file to read

        fname overwrites self.filename.
        """

        self.filename = fname

        try:
            with open(fname) as fp:
                self.layers = json.load(fp)
        except IOError, e:
            msg = 'Error opening %s: %s' % (fname, str(e))
            raise IOError(msg)

    def Write(self, fname=None):
        """Write the Resource to disk.

        fname  path to file to write (default is load self.filename)

        If fname is supplied, it overwrites self.filename.
        """

        if fname:
            self.filename = fname

        if not self.filename:
            raise Exception('Write() called but no filename supplied')

        with open(self.filename, 'wb') as fp:
            json.dump(self.layers, fp, ensure_ascii=False,
                      indent=2, separators=(',', ':'))

    def AddLayer(self, name, layer_type, data):
        """Add a layer to the Resource.

        name        name of the layer
        layer_type  type of the layer
        data        layer data
        """

        self.layers[name] = (layer_type, data)

    def GetLayers(self):
        """Get layers object.

        Returns a dict: {'layer_name': <layer_data>, ...}
        """

        return self.layers

    def GetLayer(self, name):
        """Get a layer by name.

        name  name of the layer to get

        Returns a tuple (layer_type, data), or None if not found.
        """

        return self.layers.get(name, None)

    def DeleteLayer(self, name):
        """Delete a layer by name.

        name  name of the layer to delete
        """

        try:
            del self.layers[name]
        except KeyError:
            pass

    def __len__(self):
        """Makes len(Resource) return number of layers held."""

        return len(self.layers)

###############################################################################
# The wxPython pySlip widget events.
# define the events that are raised by the pySlip widget
###############################################################################

# point/box select
_myEVT_PYSLIP_SELECT = wx.NewEventType()
EVT_PYSLIP_SELECT = wx.PyEventBinder(_myEVT_PYSLIP_SELECT, 1)

# point RIGHT select
_myEVT_PYSLIP_RIGHTSELECT = wx.NewEventType()
EVT_PYSLIP_RIGHTSELECT = wx.PyEventBinder(_myEVT_PYSLIP_RIGHTSELECT, 1)

# level change
_myEVT_PYSLIP_LEVEL = wx.NewEventType()
EVT_PYSLIP_LEVEL = wx.PyEventBinder(_myEVT_PYSLIP_LEVEL, 1)

# mouse geo position change
_myEVT_PYSLIP_POSITION = wx.NewEventType()
EVT_PYSLIP_POSITION = wx.PyEventBinder(_myEVT_PYSLIP_POSITION, 1)


class _PySlipEvent(wx.PyCommandEvent):
    """Event sent from the pySlip widget."""

    def __init__(self, eventType, id):
        """Construct a PySlip event.

        eventType  type of event
        id         unique event number

        Event will be adorned with attributes by raising code.
        """

        wx.PyCommandEvent.__init__(self, eventType, id)

###############################################################################
# The wxPython pySlip widget proper
###############################################################################

class PySlip(_BufferedCanvas):
    """A widget to display a tiled map, à la Google maps."""

    # keep a temporary list of placement dictionaries for later compilation
    placements = []

    # dictionary for map-relative image placement
    # assumes variables x, y, w, h, w2, h2, x_off & y_off are set
    # perturbs x and y to top-left image corner for placing
    image_map_placement = {'cc': 'x=x-w2+x_off;  y=y-h2+y_off',
                           'nw': 'x=x+x_off;     y=y+y_off',
                           'cn': 'x=x-w2+x_off;  y=y+y_off',
                           'ne': 'x=x-w+x_off;   y=y+y_off',
                           'ce': 'x=x-w+x_off;   y=y-h2+y_off',
                           'se': 'x=x-w+x_off;   y=y-h+y_off',
                           'cs': 'x=x-w2+x_off;  y=y-h+y_off',
                           'sw': 'x=x+x_off;     y=y-h+y_off',
                           'cw': 'x=x+x_off;     y=y-h2+y_off'}
    placements.append(image_map_placement)

    # dictionary for view-relative image placement
    # assumes variables x, y, w, h, dc_w, dc_h, x_off, y_off are set
    # perturbs x and y to top-left image corner for drawing
    image_view_placement = {'cc': 'x=dc_w2-w2;     y=dc_h2-h2',
                            'nw': 'x=x_off;        y=y_off',
                            'cn': 'x=dc_w2-w2;     y=y_off',
                            'ne': 'x=dc_w-w-x_off; y=y_off',
                            'ce': 'x=dc_w-w-x_off; y=dc_h2-h2',
                            'se': 'x=dc_w-w-x_off; y=dc_h-h-y_off',
                            'cs': 'x=dc_w2-w2;     y=dc_h-h-y_off',
                            'sw': 'x=x_off;        y=dc_h-h-y_off',
                            'cw': 'x=x_off;        y=dc_h2-h2'}
    placements.append(image_view_placement)

    # map-relative text placement dictionary
    # assumes variables x, y, w, h, dc_w, dc_h, x_off, y_off are set
    # w and h are text width and height
    # perturbs x and y to correct values for the placement
    text_map_placement = {'cc': 'x=x-w2;      y=y-h2',
                          'nw': 'x=x+x_off;   y=y+y_off',
                          'cn': 'x=x-w2;      y=y+y_off',
                          'ne': 'x=x-w-x_off; y=y+y_off',
                          'ce': 'x=x-w-x_off; y=y-h2',
                          'se': 'x=x-w-x_off; y=y-h-y_off',
                          'cs': 'x=x-w2;      y=y-h-y_off',
                          'sw': 'x=x+x_off;   y=y-h-y_off',
                          'cw': 'x=x+x_off;   y=y-h2'}
    placements.append(text_map_placement)

    # view-relative text placement dictionary
    # assumes variables x, y, w, h, dc_w, dc_h, x_off, y_off are set
    # w and h are text width and height
    # perturbs x and y to correct values for the placement
    text_view_placement = {'cc': 'x=x+dc_w2-w2; y=y+dc_h2-h2',
                           'nw': 'x=x;          y=y',
                           'cn': 'x=x+dc_w2-w2; y=y',
                           'ne': 'x=x+dc_w-w;   y=y',
                           'ce': 'x=x+dc_w-w;   y=y+dc_h2-h2',
                           'se': 'x=x+dc_w-w;   y=y+dc_h-h',
                           'cs': 'x=x+dc_w2-w2; y=y+dc_h-h',
                           'sw': 'x=x;          y=y+dc_h-h',
                           'cw': 'x=x;          y=y+dc_h2-h2'}
    placements.append(text_view_placement)

    # view-relative polygon placement dictionary
    # assumes variables x, y, dc_w, dc_h, x_off, y_off are set
    # perturbs x and y to correct values for the placement
    poly_view_placement = {'cc': 'x=x+dc_w2;      y=y+dc_h2',
                           'nw': 'x=x+x_off;      y=y+y_off',
                           'cn': 'x=x+dc_w2;      y=y+y_off',
                           'ne': 'x=x+dc_w-x_off; y=y+y_off',
                           'ce': 'x=x+dc_w-x_off; y=y+dc_h2-y_off',
                           'se': 'x=x+dc_w-x_off; y=y+dc_h-y_off',
                           'cs': 'x=x+dc_w2;      y=y+dc_h-y_off',
                           'sw': 'x=x+x_off;      y=y+dc_h-y_off',
                           'cw': 'x=x+x_off;      y=y+dc_h2'}
    placements.append(poly_view_placement)

    # dictionary for view-relative point placement
    # assumes variables x, y, dc_w, dc_h, x_off, y_off are set
    # perturbs x and y to point centre for drawing
    point_view_placement = {'cc': 'x=x+dc_w2;      y=y+dc_h2',
                            'nw': 'x=x+x_off;      y=y+y_off',
                            'cn': 'x=x+dc_w2;      y=y+y_off',
                            'ne': 'x=x+dc_w-x_off; y=y+y_off',
                            'ce': 'x=x+dc_w-x_off; y=y+dc_h2',
                            'se': 'x=x+dc_w-x_off; y=y+dc_h-y_off',
                            'cs': 'x=x+dc_w2;      y=y+dc_h-y_off',
                            'sw': 'x=x+x_off;      y=y+dc_h-y_off',
                            'cw': 'x=x+x_off;      y=y+dc_h2'}
    placements.append(point_view_placement)

    # now pre-compile all the dictionary placement strings
    for p_dict in placements:
        for key in p_dict:
            p_dict[key] = compile(p_dict[key], 'string', 'exec')
    del placements

    # panel background colour
    BackgroundColour = wx.WHITE

    # default text attributes - map relative
    DefaultTextPlacement = 'se'
    DefaultTextRadius = 2
    DefaultTextColour = wx.BLACK
    DefaultTextTextColour = wx.BLACK
    DefaultTextOffsetX = 5
    DefaultTextOffsetY = 1
    DefaultTextFontname = 'Arial'
    DefaultTextFontSize = 9
    DefaultTextData = None

    # default text attributes - view relative
    DefaultTextViewPlacement = 'se'
    DefaultTextViewRadius = 0
    DefaultTextViewColour = wx.BLACK
    DefaultTextViewTextColour = wx.BLACK
    DefaultTextViewOffsetX = 0
    DefaultTextViewOffsetY = 0
    DefaultTextViewFontname = 'Arial'
    DefaultTextViewFontSize = 9
    DefaultTextViewData = None

    # default point attributes
    DefaultPointPlacement = 'nw'
    DefaultPointRadius = 3
    DefaultPointColour = wx.RED
    DefaultPointOffsetX = 0
    DefaultPointOffsetY = 0
    DefaultPointData = None

    # default image attributes
    DefaultImagePlacement = 'cc'
    DefaultImageRadius = 0
    DefaultImageColour = wx.BLACK
    DefaultImageOffsetX = 0
    DefaultImageOffsetY = 0

    # default polygon attributes
    DefaultPolygonPlacement = 'cc'
    DefaultPolygonWidth = 1
    DefaultPolygonColour = wx.RED
    DefaultPolygonClose = False
    DefaultPolygonFilled = False
    DefaultPolygonFillcolour = 'blue'
    DefaultPolygonOffsetX = 0
    DefaultPolygonOffsetY = 0

    # layer type values
    TypePoint = 0
    TypeImage = 1
    TypePolygon = 2
    TypeText = 3

    def __init__(self, parent, tile_src=None, start_level=None,
                 min_level=None, max_level=None, **kwargs):
        """Initialise a pySlip instance.

        parent       reference to parent object
        tile_src     the Tiles source object
        start_level  initial tile level to start at
        min_level    the minimum tile level to use
        max_level    the maximum tile level to use
        **kwargs     keyword args for Panel
        """

        # create and initialise the base panel
        _BufferedCanvas.__init__(self, parent=parent, **kwargs)
        self.SetBackgroundColour(PySlip.BackgroundColour)

        # save tile source object
        self.tiles = tile_src

        self.max_level = max_level if max_level else self.tiles.max_level
        self.min_level = min_level if min_level else self.tiles.min_level
        self.level = start_level if start_level else self.min_level

        self.tile_size_x = self.tiles.tile_size_x
        self.tile_size_y = self.tiles.tile_size_y

        # set some internal state
        self.view_width = None          # view size in pixels
        self.view_height = None

        self.map_width = None           # map size in pixels
        self.map_height = None

        self.ppd_x = 0                  # pixel_per_degree for current tileset
        self.ppd_y = 0

        self.view_offset_x = 0          # map pixel offset at left & top of view
        self.view_offset_y = 0

        self.view_llon = self.view_rlon = None  # view limits
        self.view_tlat = self.view_blat = None

        self.was_dragging = False               # True if dragging map
        self.move_dx = 0                        # drag delta values
        self.move_dy = 0
        self.last_drag_x = None                 # previous drag position
        self.last_drag_y = None

        self.ignore_next_up = False             # ignore next LEFT UP event
        self.ignore_next_right_up = False       # ignore next RIGHT UP event

        self.is_box_select = False              # True if box selection
        self.sbox_1_x = self.sbox_1_y = None    # box size

        # layer stuff
        self.next_layer_id = 1      # source of unique layer IDs
        self.layer_z_order = []     # layer Z order, contains layer IDs
        self.layer_mapping = {}     # maps layer ID to layer data

        # True if we send event to report mouse position in view
        self.mouse_position_event = True

        # True if event on right mouse click (right button up event)
        self.right_click_event = False

        # True if we send event on level change
        self.change_level_event = True

        # OK, use the tile level the user wants
        self.ZoomToLevel(self.level)

        # set up dispatch dictionary for layer select handlers
        # for point select
        self.layerPSelHandler = {self.TypePoint: self.GetNearestPointInLayer,
                                 self.TypeImage: self.GetNearestImageInLayer,
                                 self.TypePolygon:
                                     self.GetNearestPolygonInLayer,
                                 self.TypeText: self.GetNearestTextInLayer}

        # for box select
        self.layerBSelHandler = {self.TypePoint: self.GetBoxSelPointsInLayer,
                                 self.TypeImage: self.GetBoxSelImagesInLayer,
                                 self.TypePolygon:
                                     self.GetBoxSelPolygonsInLayer,
                                 self.TypeText: self.GetBoxSelTextsInLayer}

        # bind event handlers
        self.Bind(wx.EVT_MOTION, self.OnMove)
        self.Bind(wx.EVT_LEFT_DOWN, self.OnLeftDown)
        self.Bind(wx.EVT_LEFT_DCLICK, self.OnLeftDClick)
        self.Bind(wx.EVT_LEFT_UP, self.OnLeftUp)
        self.Bind(wx.EVT_MIDDLE_DOWN, self.OnMiddleDown)
        self.Bind(wx.EVT_MIDDLE_UP, self.OnMiddleUp)
        self.Bind(wx.EVT_RIGHT_DOWN, self.OnRightDown)
        self.Bind(wx.EVT_RIGHT_DCLICK, self.OnRightDClick)
        self.Bind(wx.EVT_RIGHT_UP, self.OnRightUp)
        self.Bind(wx.EVT_MOUSEWHEEL, self.OnMouseWheel)
        self.Bind(wx.EVT_ENTER_WINDOW, self.OnEnterWindow)
        self.Bind(wx.EVT_LEAVE_WINDOW, self.OnLeaveWindow)

        # set callback from Tile source object when tile(s) available
        self.tiles.SetAvailableCallback(self.OnTileAvailable)

        # set callback when parent resizes
        self.onSizeCallback = self.ResizeCallback

        # force a resize, which sets up the rest of the state
        self.OnSize()

    def OnTileAvailable(self, level, x, y, img, pic):
        """Callback routine: tile level/x/y is available.

        Just redraw the entire canvas.
        """

        self.Update()

    def OnEnterWindow(self, event):
        """Event handler when mouse enters widget."""

        pass

    def OnLeaveWindow(self, event):
        """Event handler when mouse leaves widget."""

        self.RaiseMousePositionEvent(None)

    ######
    # "add a layer" routines
    ######

    def AddPointLayer(self, points, map_rel=True, visible=True,
                      show_levels=None, selectable=False,
                      name='<points_layer>', **kwargs):
        """Add a layer of points.

        points       iterable of point data:
                         (x, y, [attributes])
                     where x & y are either lon&lat (map) or x&y (view) coords
                     and attributes is an optional dictionary of attributes for
                     each point with keys like:
                         'placement'  a placement string
                         'radius'     radius of point in pixels
                         'colour'     colour of point
                         'offset_x'   X offset
                         'offset_y'   Y offset
                         'data'       point data object
        map_rel      points are map relative if True, else view relative
        visible      True if the layer is to be immediately visible
        show_levels  list of levels at which layer is auto-shown (or None)
        selectable   True if select operates on this layer
        name         the 'name' of the layer - mainly for debug
        kwargs       a layer-specific attributes dictionary, has keys:
                         'placement'  a placement string
                         'radius'     radius of point in pixels
                         'colour'     colour of point
                         'offset_x'   X offset
                         'offset_y'   Y offset
                         'data'       point data object
        """

        # get global values
        default_placement = kwargs.get('placement',
                                       self.DefaultPointPlacement)
        default_radius = kwargs.get('radius', self.DefaultPointRadius)
        default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                          self.DefaultPointColour)
        default_offset_x = kwargs.get('offset_x', self.DefaultPointOffsetX)
        default_offset_y = kwargs.get('offset_y', self.DefaultPointOffsetY)
        default_data = kwargs.get('data', self.DefaultPointData)

        # create data iterable for draw method
        draw_data = []              # list to hold draw data

        for pt in points:
            if len(pt) == 3:
                (x, y, attributes) = pt
            elif len(pt) == 2:
                (x, y) = pt
                attributes = {}
            else:
                msg = ('Points data must be iterable of tuples: '
                       '(x, y, [dict])\n'
                       'Got: %s' % str(pt))
                raise Exception(msg)

            # plug in any required layer defaults (override globals)
            placement = attributes.get('placement', default_placement)
            radius = attributes.get('radius', default_radius)
            colour = self.get_i18n_kw(attributes, ('colour', 'color'),
                                      default_colour)
            offset_x = attributes.get('offset_x', default_offset_x)
            offset_y = attributes.get('offset_y', default_offset_y)
            data = attributes.get('data', default_data)

            draw_data.append((x, y, placement.lower(),
                              radius, colour, offset_x, offset_y, data))

        return self.AddLayer(self.DrawPointLayer, draw_data, map_rel,
                             visible=visible, show_levels=show_levels,
                             selectable=selectable, name=name,
                             type=self.TypePoint)

    def AddPolygonLayer(self, data, map_rel=True, visible=True,
                        show_levels=None, selectable=False,
                        name='<polygon_layer>', **kwargs):
        """Add a layer of polygon data to the map.

        data         iterable of polygon tuples:
                         (<iter>[, attributes])
                     where <iter> is another iterable of (x, y) tuples and
                     attributes is a dictionary of polygon attributes:
                         placement   a placement string (view-relative only)
                         width       width of polygon edge lines
                         colour      colour of edge lines
                         close       if True closes polygon
                         filled      polygon is filled (implies closed)
                         fillcolour  fill colour
                         offset_x    X offset
                         offset_y    Y offset
                         data        polygon data object
        map_rel      points drawn relative to map if True, else view relative
        visible      True if the layer is to be immediately visible
        show_levels  list of levels at which layer is auto-shown (or None)
        selectable   True if select operates on this layer
        name         name of this layer
        kwargs       extra keyword args, layer-specific:
                         placement   placement string (view-rel only)
                         width       width of polygons in pixels
                         colour      colour of polygon edge lines
                         close       True if polygon is to be closed
                         filled      if True, fills polygon
                         fillcolour  fill colour
                         offset_x    X offset
                         offset_y    Y offset
        """

        # get global values, if required
        default_placement = kwargs.get('placement',
                                       self.DefaultPolygonPlacement)
        default_width = kwargs.get('width', self.DefaultPolygonWidth)
        default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                          self.DefaultPolygonColour)
        default_close = kwargs.get('closed', self.DefaultPolygonClose)
        default_filled = kwargs.get('filled', self.DefaultPolygonFilled)
        default_fillcolour = self.get_i18n_kw(kwargs,
                                              ('fillcolour', 'fillcolor'),
                                              self.DefaultPolygonFillcolour)
        default_offset_x = kwargs.get('offset_x', self.DefaultPolygonOffsetX)
        default_offset_y = kwargs.get('offset_y', self.DefaultPolygonOffsetY)

        # create draw_data iterable
        draw_data = []
        for d in data:
            if len(d) == 2:
                (p, attributes) = d
            elif len(d) == 1:
                p = d
                attributes = {}
            else:
                msg = ('Polygon data must be iterable of tuples: '
                       '(poly, [attributes])\n'
                       'Got: %s' % str(d))
                raise Exception(msg)

            # get polygon attributes
            placement = attributes.get('placement', default_placement)
            width = attributes.get('width', default_width)
            colour = self.get_i18n_kw(attributes, ('colour', 'color'),
                                      default_colour)
            close = attributes.get('closed', default_close)
            filled = attributes.get('filled', default_filled)
            if filled:
                close = True
            fillcolour = self.get_i18n_kw(attributes,
                                          ('fillcolour', 'fillcolor'),
                                          default_fillcolour)
            offset_x = attributes.get('offset_x', default_offset_x)
            offset_y = attributes.get('offset_y', default_offset_y)
            data = attributes.get('data', None)

            # if polygon is to be filled, ensure closed
            if close:
                p = list(p)
                p.append(p[0])

            draw_data.append((p, placement.lower(), width, colour, close,
                              filled, fillcolour, offset_x, offset_y, data))

        return self.AddLayer(self.DrawPolygonLayer, draw_data, map_rel,
                             visible=visible, show_levels=show_levels,
                             selectable=selectable, name=name,
                             type=self.TypePolygon)

    def AddImageLayer(self, data, map_rel=True, visible=True,
                      show_levels=None, selectable=False,
                      name='<image_layer>', **kwargs):
        """Add a layer of images to the map.

        data         list of (lon, lat, fname[, attributes]) (map_rel)
                     or list of (x, y, fname, [attributes]) (view relative)
                     attributes is a dictionary of attribute keys:
                         placement  a placement string
                         offset_x   X offset
                         offset_y   Y offset
                         data       image data object
        map_rel      points drawn relative to map if True, else view relative
        visible      True if the layer is to be immediately visible
        show_levels  list of levels at which layer is auto-shown (or None)
        selectable   True if select operates on this layer
        name         name of this layer
        kwargs       dictionary of extra params:
                         placement  string describing placement wrt hotspot
                         offset_x   hotspot X offset in pixels
                         offset_y   hotspot Y offset in pixels

        The hotspot is placed at (lon, lat) or (x, y).  'placement' controls
        where the image is displayed relative to the hotspot.
        """

        # get global attribute values
        default_placement = kwargs.get('placement',
                                       self.DefaultImagePlacement)
        default_offset_x = kwargs.get('offset_x', self.DefaultImageOffsetX)
        default_offset_y = kwargs.get('offset_y', self.DefaultImageOffsetY)

        # define cache variables for the image data
        fname_cache = None
        bmp_cache = None
        w_cache = None
        h_cache = None          # used to optimize file access

        # load all image files, convert to bitmaps, create draw_data iterable
        draw_data = []
        for d in data:
            if len(d) == 4:
                (lon, lat, fname, attributes) = d
            elif len(d) == 3:
                (lon, lat, fname) = d
                attributes = {}
            else:
                msg = ('Points data must be iterable of tuples: '
                       '(x, y, [dict])\nGot: %s' % str(d))
                raise Exception(msg)

            placement = attributes.get('placement', default_placement)
            offset_x = attributes.get('offset_x', default_offset_x)
            offset_y = attributes.get('offset_y', default_offset_y)
            data = attributes.get('data', None)

            if fname == fname_cache:
                bmap = bmp_cache
                w = w_cache
                h = h_cache
            else:
                fname_cache = fname
                log('fname=%s' % str(fname))
                img = wx.Image(fname, wx.BITMAP_TYPE_ANY)
                bmp_cache = bmap = img.ConvertToBitmap()
                (w, h) = bmap.GetSize()
                w_cache = w
                h_cache = h
            draw_data.append((lon, lat, bmap, w, h, placement.lower(),
                              offset_x, offset_y, data))

        return self.AddLayer(self.DrawImageLayer, draw_data, map_rel,
                             visible=visible, show_levels=show_levels,
                             selectable=selectable, name=name,
                             type=self.TypeImage)

    def AddTextLayer(self, text, map_rel=True, visible=True, show_levels=None,
                     selectable=False, name='<text_layer>', **kwargs):
        """Add a text layer to the map.

        text         list of sequence of (lon, lat, text, [dict]) coordinates
        map_rel      points drawn relative to map if True, else view relative
        visible      True if the layer is to be immediately visible
        show_levels  list of levels at which layer is auto-shown
        selectable   True if select operates on this layer
        name         name of this layer
        kwargs       a dictionary of changeable text attributes
                         (placement, radius, fontname, fontsize, colour, data)
                     these supply any data missing in 'data'
        """

        if map_rel:
            default_placement = kwargs.get('placement', self.DefaultTextPlacement)
            default_radius = kwargs.get('radius', self.DefaultTextRadius)
            default_fontname = kwargs.get('fontname', self.DefaultTextFontname)
            default_fontsize = kwargs.get('fontsize', self.DefaultTextFontSize)
            default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                              self.DefaultTextColour)
            default_textcolour = self.get_i18n_kw(kwargs,
                                                  ('textcolour', 'textcolor'),
                                                  self.DefaultTextTextColour)
            default_offset_x = kwargs.get('offset_x', self.DefaultTextOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultTextOffsetY)
            default_data = kwargs.get('data', self.DefaultTextData)
        else:
            default_placement = kwargs.get('placement', self.DefaultTextViewPlacement)
            default_radius = kwargs.get('radius', self.DefaultTextViewRadius)
            default_fontname = kwargs.get('fontname', self.DefaultTextViewFontname)
            default_fontsize = kwargs.get('fontsize', self.DefaultTextViewFontSize)
            default_colour = self.get_i18n_kw(kwargs, ('colour', 'color'),
                                              self.DefaultTextViewColour)
            default_textcolour = self.get_i18n_kw(kwargs,
                                                  ('textcolour', 'textcolor'),
                                                  self.DefaultTextViewTextColour)
            default_offset_x = kwargs.get('offset_x', self.DefaultTextViewOffsetX)
            default_offset_y = kwargs.get('offset_y', self.DefaultTextViewOffsetY)
            default_data = kwargs.get('data', self.DefaultTextData)

        # create data iterable ready for drawing
        draw_data = []
        for t in text:
            if len(t) == 4:
                (lon, lat, tdata, attributes) = t
            elif len(t) == 3:
                (lon, lat, tdata) = t
                attributes = {}
            else:
                msg = ('Text data must be iterable of tuples: '
                       '(lon, lat, text, [dict])\n'
                       'Got: %s' % str(t))
                raise Exception(msg)

            # plug in any required defaults
            placement = attributes.get('placement', default_placement)
            radius = attributes.get('radius', default_radius)
            fontname = attributes.get('fontname', default_fontname)
            fontsize = attributes.get('fontsize', default_fontsize)
            colour = self.get_i18n_kw(attributes, ('colour', 'color'),
                                      default_colour)
            textcolour = self.get_i18n_kw(attributes,
                                          ('textcolour', 'textcolor'),
                                          default_textcolour)
            offset_x = attributes.get('offset_x', default_offset_x)
            offset_y = attributes.get('offset_y', default_offset_y)
            data = attributes.get('data', default_data)

            draw_data.append((lon, lat, tdata, placement.lower(),
                              radius, colour, textcolour, fontname, fontsize,
                              offset_x, offset_y, data))

        return self.AddLayer(self.DrawTextLayer, draw_data, map_rel,
                             visible=visible, show_levels=show_levels,
                             selectable=selectable, name=name,
                             type=self.TypeText)

    def AddLayer(self, render, data, map_rel, visible, show_levels,
                 selectable, name, type):
        """Add a generic layer to the system.

        render       the function used to render the layer
        data         actual layer data (depends on layer type)
        map_rel      True if points are map relative, else view relative
        visible      True if layer is to be immediately shown, else False
        show_levels  list of levels at which to auto-show the layer
        selectable   True if select operates on this layer
        name         name for this layer
        type         flag for layer 'type'

        Returns unique ID of the new layer.
        """

        # get layer ID
        id = self.next_layer_id
        self.next_layer_id += 1

        # prepare the show_level value
        if show_levels is None:
            show_levels = range(self.min_level, self.max_level + 1)[:]

        # create layer, add unique ID to Z order list
        l = _Layer(id=id, painter=render, data=data, map_rel=map_rel,
                   visible=visible, show_levels=show_levels,
                   selectable=selectable, name=name, type=type)

        self.layer_mapping[id] = l
        self.layer_z_order.append(id)

        # force display of new layer if it's visible
        if visible:
            self.Update()

        return id

    ######
    # Layer manipulation routines.
    ######

    def ShowLayer(self, id):
        """Show a layer.

        id  the layer id
        """

        self.layer_mapping[id].visible = True
        self.Update()

    def HideLayer(self, id):
        """Hide a layer.

        id  the layer id
        """

        self.layer_mapping[id].visible = False
        self.Update()

    def DeleteLayer(self, id):
        """Delete a layer.

        id  the layer id
        """

        # just in case we got None
        if id:
            # see if what we are about to remove might be visible
            layer = self.layer_mapping[id]
            visible = layer.visible

            del layer
            self.layer_z_order.remove(id)

            # if layer was visible, refresh display
            if visible:
                self.Update()

    def SetLayerShowLevels(self, id, show_levels=None):
        """Update the show_levels list for a layer.

        id           ID of the layer we are going to update
        show_levels  new layer show list
        """

        # just in case we got None
        if id:
            layer = self.layer_mapping[id]

            # prepare the show_level value
            if show_levels is None:
                show_levels = range(self.min_level, self.max_level + 1)[:]

            layer.show_levels = show_levels

            # if layer was visible, refresh display
            if visible:
                self.Update()

    def SetLayerSelectable(self, id, selectable=False):
        """Update the .selectable attribute for a layer.

        id          ID of the layer we are going to update
        selectable  new .selectable attribute value (True or False)
        """

        # just in case we got None
        if id:
            layer = self.layer_mapping[id]
            layer.selectable = selectable

    ######
    # Play with layers Z order
    ######

    def PushLayerToBack(self, id):
        """Make layer specified be drawn at back of Z order.

        id  ID of the layer to push to the back
        """

        self.layer_z_order.remove(id)
        self.layer_z_order.insert(0, id)
        self.Update()

    def PopLayerToFront(self, id):
        """Make layer specified be drawn at front of Z order.

        id  ID of the layer to pop to the front
        """

        self.layer_z_order.remove(id)
        self.layer_z_order.append(id)
        self.Update()

    def PlaceLayerBelowLayer(self, id, top_id):
        """Place a layer so it will be drawn behind another layer.

        id      ID of layer to place underneath 'top_id'
        top_id  ID of layer to be drawn *above* 'id'
        """

        self.layer_z_order.remove(id)
        i = self.layer_z_order.index(top_id)
        self.layer_z_order.insert(i, id)
        self.Update()

    ######
    # Layer drawing routines
    ######

    def DrawPointLayer(self, dc, data, map_rel):
        """Draw a points layer.

        dc       the device context to draw on
        data     an iterable of point tuples:
                     (x, y, place, radius, colour, x_off, y_off, pdata)
        map_rel  points relative to map if True, else relative to view
        """

        # draw points on map/view
        if map_rel:
            dc = wx.GCDC(dc)		# allow transparent colours
            for (lon, lat, place,
                 radius, colour, x_off, y_off, pdata) in data:
                pt = self.ConvertGeo2ViewMasked((lon, lat))
                if pt:
                    dc.SetPen(wx.Pen(colour))
                    dc.SetBrush(wx.Brush(colour))
                    (x, y) = pt
                    if radius:
                        dc.DrawCircle(x + x_off, y + y_off, radius)
        else:
            (dc_w, dc_h) = dc.GetSize()
            dc_w2 = dc_w / 2
            dc_h2 = dc_h / 2
            dc_h -= 1
            dc_w -= 1
            dc = wx.GCDC(dc)		# allow transparent colours
            for (x, y, place, radius, colour, x_off, y_off, pdata) in data:
                dc.SetPen(wx.Pen(colour))
                dc.SetBrush(wx.Brush(colour))
                exec self.point_view_placement[place]
                if radius:
                    dc.DrawCircle(x, y, radius)

    def DrawPolygonLayer(self, dc, data, map_rel):
        """Draw a polygon layer.

        dc       the device context to draw on
        data     an iterable of polygon tuples:
                     (p, placement, width, colour, closed,
                      filled, fillcolour, offset_x, offset_y, data)
                 where p is an iterable of points: (x, y)
        map_rel  points relative to map if True, else relative to view
        """

        # draw polygons on map/view
        if map_rel:
            dc = wx.GCDC(dc)		# allow transparent colours
            for (p, place, width, colour, closed,
                 filled, fillcolour, x_off, y_off, pdata) in data:
                # gather all polygon points as view coords
                p_lonlat = []
                for lonlat in p:
                    (lon, lat) = lonlat
                    (x, y) = self.tiles.Geo2Tile(lat, lon)
#                    (x, y) = self.tiles.ConvertGeo2View(lonlat,
#                                                        self.view_offset_x,
#                                                        self.view_offset_y,
#                                                        self.view_llon,
#                                                        self.view_tlat,
#                                                        self.ppd_x,
#                                                        self.ppd_y)
                    p_lonlat.append((x - self.view_offset_x + x_off,
                                     y - self.view_offset_y + y_off))

                dc.SetPen(wx.Pen(colour, width=width))

                if filled:
                    dc.SetBrush(wx.Brush(fillcolour))
                else:
                    dc.SetBrush(wx.TRANSPARENT_BRUSH)

                if closed:
                    dc.DrawPolygon(p_lonlat)
                else:
                    dc.DrawLines(p_lonlat)
        else:
            (dc_w, dc_h) = dc.GetSize()
            dc_w2 = dc_w / 2
            dc_h2 = dc_h / 2
            dc_w -= 1
            dc_h -= 1
            dc = wx.GCDC(dc)		# allow transparent colours
            for (p, place, width, colour, closed,
                 filled, fillcolour, x_off, y_off, pdata) in data:
                # fetch the exec code, don't refetch for each point in polygon
                place_exec = self.poly_view_placement[place]
                pp = []
                for (x, y) in p:
                    exec place_exec
                    pp.append((x, y))

                dc.SetPen(wx.Pen(colour, width=width))

                if filled:
                    dc.SetBrush(wx.Brush(fillcolour))
                else:
                    dc.SetBrush(wx.TRANSPARENT_BRUSH)

                if closed:
                    dc.DrawPolygon(pp)
                else:
                    dc.DrawLines(pp)

    def DrawImageLayer(self, dc, images, map_rel):
        """Draw an image Layer on the view.

        dc       the device context to draw on
        images   a sequence of image tuple sequences
                   (x,y,bmap,w,h,placement,offset_x,offset_y,idata)
        map_rel  points relative to map if True, else relative to view
        """

        # draw images on map/view
        if map_rel:
            for (lon, lat, bmap, w, h, place, x_off, y_off, idata) in images:
                w2 = w / 2
                h2 = h / 2
                pt = self.ConvertGeo2ViewMasked((lon, lat))
                if pt:
                    (x, y) = pt
                    exec self.image_map_placement[place]
                    dc.DrawBitmap(bmap, x, y, False)
        else:
            (dc_w, dc_h) = dc.GetSize()
            dc_w2 = dc_w / 2
            dc_h2 = dc_h / 2
            for (x, y, bmap, w, h, place, x_off, y_off, idata) in images:
                w2 = w / 2
                h2 = h / 2
                exec self.image_view_placement[place]
                dc.DrawBitmap(bmap, x, y, False)

    def DrawTextLayer(self, dc, text, map_rel):
        """Draw a text Layer on the view.

        dc       the device context to draw on
        text     a sequence of tuples:
                     (lon, lat, tdata, placement, radius, colour, fontname,
                      fontsize, offset_x, offset_y, tdata)
        map_rel  points relative to map if True, else relative to view
        """

        if text is None:
            return

        # draw text on map/view
        if map_rel:
            dc = wx.GCDC(dc)		# allow transparent colours
            for t in text:
                (lon, lat, tdata, place, radius, colour, textcolour,
                     fontname, fontsize, x_off, y_off, data) = t

                # convert geo position to view (returns None if off-view)
                pt = self.ConvertGeo2ViewMasked((lon, lat))
                if pt:
                    (x, y) = pt

                    # set font characteristics
                    dc.SetPen(wx.Pen(colour))
                    dc.SetBrush(wx.Brush(colour))
                    dc.SetTextForeground(colour)
                    font = wx.Font(fontsize, wx.SWISS, wx.NORMAL, wx.NORMAL,
                                   False, fontname)
                    dc.SetFont(font)

                    # draw hotpoint circle
                    if radius:
                        dc.DrawCircle(x, y, radius)

                    # place the text relative to hotpoint
                    (w, h, _, _) = dc.GetFullTextExtent(tdata)
                    w2 = w / 2
                    h2 = h / 2
                    exec self.text_map_placement[place]
                    dc.SetTextForeground(textcolour)
                    dc.DrawText(tdata, x, y)
        else:
            # we need the size of the DC
            (dc_w, dc_h) = dc.GetSize()
            dc_w2 = dc_w / 2
            dc_h2 = dc_h / 2
            dc_w -= 1
            dc_h -= 1
            dc = wx.GCDC(dc)		# allow transparent colours
            for t in text:
                # for each text element, get unpacked data
                (x, y, tdata, place, radius, colour, textcolour,
                     fontname, fontsize, x_off, y_off, data) = t

                # set font characteristics
                dc.SetPen(wx.Pen(colour))
                dc.SetBrush(wx.Brush(colour))
                dc.SetTextForeground(colour)
                font = wx.Font(fontsize, wx.SWISS, wx.NORMAL, wx.NORMAL,
                               False, fontname)
                dc.SetFont(font)

                # draw hotpoint circle - do placement with x & y zero
                (save_x, save_y) = (x, y)
                (w, h, w2, h2, x, y) = (0, 0, 0, 0, 0, 0)
                exec self.text_view_placement[place]
                if radius:
                    dc.DrawCircle(x, y, radius)
                (x, y) = (save_x, save_y)

                # place the text relative to hotpoint
                (w, h, _, _) = dc.GetFullTextExtent(tdata)  # size of text
                w2 = w / 2
                h2 = h / 2
                exec self.text_view_placement[place]
                dc.SetTextForeground(textcolour)
                dc.DrawText(tdata, x, y)

    ######
    # Positioning methods
    ######

    def GotoPosition(self, posn):
        """Set view to centre on a position in the current level.

        posn  a tuple (lon,lat) to centre view on
        """

        (lon, lat) = posn
        log('GotoPosition: lon=%.4f, lat=%.4f' % (lon, lat))

        # get fractional tile coords of centre of view
        (xtile, ytile) = self.tiles.Geo2Tile(lat, lon)
#        (xtile, ytile) = self.tiles.ConvertGeo2TileCoords(lat, lon, self.level,
#                                                          ppd_x=self.ppd_x,
#                                                          ppd_y=self.ppd_y,
#                                                          map_tlat=self.map_tlat,
#                                                          map_blat=self.map_blat,
#                                                          map_llon=self.map_llon,
#                                                          map_rlon=self.map_rlon)
        log('GotoPosition: xtile=%s, ytile=%s' % (str(xtile), str(ytile)))

        # now calculate view offsets, top, left, bottom and right
        half_width = self.view_width / 2
        centre_pixels_from_map_left = int(xtile * self.tile_size_x)
        self.view_offset_x = centre_pixels_from_map_left - half_width
        log('GotoPosition: self.view_width=%d, centre_pixels_from_map_left=%d, self.view_offset_x=%d'
                % (self.view_width, centre_pixels_from_map_left, self.view_offset_x))

        half_height = self.view_height / 2
        centre_pixels_from_map_top = int(ytile * self.tile_size_y)
        self.view_offset_y = centre_pixels_from_map_top - half_height
        log('GotoPosition: self.view_height=%d, centre_pixels_from_map_top=%d, self.view_offset_y=%d'
                % (self.view_height, centre_pixels_from_map_top, self.view_offset_y))

        # set the left/right/top/bottom lon/lat extents
        self.RecalcViewLonLatLimits()

        self.Update()

    def GotoLevelAndPosition(self, level, posn):
        """Goto a map level and set view to centre on a position.

        level  the map level to use
        posn   a tuple (lon,lat) to centre view on

        Does nothing if we can't use desired level.
        """

        if self.ZoomToLevel(level):
            self.GotoPosition(posn)

    def ZoomToArea(self, posn, size):
        """Set view to level and position to view an area.

        posn  a tuple (lon,lat) to centre view on
        size  a tuple (width,height) of area in degrees

        Centre an area and zoom to view such that the area will fill
        approximately 50% of width or height, whichever is greater.

        Use the ppd_x and ppd_y values in the level 'tiles.info' file.
        """

        # unpack area width/height (degrees)
        (awidth, aheight) = size

        # step through levels (smallest first) and check view size (degrees)
        for l in self.tiles.levels:
            level = l
            (_, _, ppd_x, ppd_y) = self.tiles.getInfo(l)
            view_deg_width = self.view_width / ppd_x
            view_deg_height = self.view_height / ppd_y

            # if area >= 50% of view, finished
            if awidth >= view_deg_width / 2 or aheight >= view_deg_height / 2:
                break

        self.GotoLevelAndPosition(level, posn)

    ######
    # Convert between geo and view coordinates
    ######

    def ConvertGeo2ViewMasked(self, lonlat):
        """Convert a geo (lon+lat) position to view pixel coords.

        lonlat  (longitude, latitude) of point

        Return screen pixels coordinates of the point (x,y) or None
        if point is off-view.
        """

        (lon, lat) = lonlat

        if (self.view_llon <= lon <= self.view_rlon and
                self.view_blat <= lat <= self.view_tlat):
            (x, y) = self.tiles.Geo2Tile(lat, lon)
            return (x - self.view_offset_x, y - self.view_offset_y)
#            return self.tiles.ConvertGeo2View(lonlat,
#                                              self.view_offset_x,
#                                              self.view_offset_y,
#                                              self.view_llon,
#                                              self.view_tlat,
#                                              self.ppd_x,
#                                              self.ppd_y)

        return None

    ######
    # GUI stuff
    ######

    def OnMove(self, event):
        """Handle a mouse move (map drag or rectangle select).

        event  the mouse move event

        If SHIFT key is down, do rectangle select.
        Otherwise pan the map if we are dragging.
        """

        # for windows, set focus onto pyslip window
        # linux seems to do this automatically
        if sys.platform == 'win32' and self.FindFocus() != self:
            self.SetFocus()

        # get current mouse position
        (x, y) = event.GetPositionTuple()

        self.RaiseMousePositionEvent((x, y))

        if event.Dragging() and event.LeftIsDown():
            # are we doing box select?
            if self.is_box_select:
                # set select box point 2 at mouse position
                (self.sbox_w, self.sbox_h) = (x - self.sbox_1_x,
                                              y - self.sbox_1_y)
            elif not self.last_drag_x is None:
                # no, just a map drag
                self.was_dragging = True
                dx = self.last_drag_x - x
                dy = self.last_drag_y - y

                # move the map in the view
                self.view_offset_x += dx
                self.view_offset_y += dy

                # limit drag at edges of map
                if self.map_width > self.view_width:
                    # if map > view, don't allow edge to show background
                    if self.view_offset_x < 0:
                        self.view_offset_x = 0
                    elif self.view_offset_x > self.max_x_offset:
                        self.view_offset_x = self.max_x_offset
                else:
                    # else map < view, centre X
                    self.view_offset_x = (self.map_width
                                          - self.view_width) / 2

                if self.map_height > self.view_height:
                    # if map > view, don't allow edge to show background
                    if self.view_offset_y < 0:
                        self.view_offset_y = 0
                    elif self.view_offset_y > self.max_y_offset:
                        self.view_offset_y = self.max_y_offset
                else:
                    # else map < view, centre Y
                    self.view_offset_y = (self.map_height
                                          - self.view_height) / 2

                # adjust remembered X,Y
                self.last_drag_x = x
                self.last_drag_y = y

                self.RecalcViewLonLatLimits()

            # redraw client area
            log('OnMove: before Update()')
            self.Update()
            log('OnMove: after Update()')

    def OnLeftDown(self, event):
        """Left mouse button down. Prepare for possible drag."""

        click_posn = event.GetPositionTuple()

        if event.ShiftDown():
            self.is_box_select = True
            self.SetCursor(wx.StockCursor(wx.CURSOR_CROSS))
            (self.sbox_w, self.sbox_h) = (0, 0)
            (self.sbox_1_x, self.sbox_1_y) = click_posn
        else:
            self.is_box_select = False
            self.SetCursor(wx.StockCursor(wx.CURSOR_HAND))
            (self.last_drag_x, self.last_drag_y) = click_posn
        event.Skip()

    def OnLeftUp(self, event):
        """Left mouse button up.

        Note that when we iterate through the layer_z_order list we must
        iterate on a *copy* as the user select process can modify
        self.layer_z_order.
        """

        self.last_drag_x = self.last_drag_y = None

        if self.ignore_next_up:
            self.ignore_next_up = False
            return

        self.SetCursor(wx.StockCursor(wx.CURSOR_DEFAULT))

        # we need a repaint to remove any selection box, but NOT YET!
        delayed_paint = self.sbox_1_x       # True if box select active

        # if any layers interested, inform of possible select
        if not self.was_dragging:
            if self.is_box_select:
                # possible box selection
#                ll_corner_v = (self.sbox_1_x, self.sbox_1_y)
#                tr_corner_v = (self.sbox_1_x + self.sbox_w,
#                               self.sbox_1_y + self.sbox_h)

                ll_corner_x = (self.sbox_1_x+self.view_offset_x) / self.tile_size_x
                ll_corner_y = (self.sbox_1_y+self.view_offset_y) / self.tile_size_y

                ll_corner_t = self.tiles.Tile2Geo(ll_corner_x, ll_corner_y)
                tr_corner_t = self.tiles.Tile2Geo(ll_corner_x + 1.0,
                                                  ll_corner_y + 1.0)

#                ll_corner_m = self.tiles.ConvertView2Geo(ll_corner_v,
#                                                         self.view_offset_x,
#                                                         self.view_offset_y,
#                                                         self.map_llon,
#                                                         self.map_tlat,
#                                                         self.ppd_x,
#                                                         self.ppd_y)
#                tr_corner_m = self.tiles.ConvertView2Geo(tr_corner_v,
#                                                         self.view_offset_x,
#                                                         self.view_offset_y,
#                                                         self.map_llon,
#                                                         self.map_tlat,
#                                                         self.ppd_x,
#                                                         self.ppd_y)

                # check each layer for a box select event
                # we work on a copy as user response could change order
                for id in self.layer_z_order[:]:
                    l = self.layer_mapping[id]
                    # if layer visible and selectable
                    if l.selectable and l.visible:
                        if l.map_rel:
                            # map-relative, get all points selected (if any)
                            p_data = self.layerBSelHandler[l.type](l,
                                                                   ll_corner_t,
                                                                   tr_corner_t)
                        else:
                            # view-relative
                            p_data = self.layerBSelHandler[l.type](l,
                                                                   ll_corner_v,
                                                                   tr_corner_v)
                        self.RaiseSelectEvent(EventBoxSelect, l, p_data)

                        # user code possibly updated screen
                        delayed_paint = True
                self.is_box_select = False
            else:
                # possible point selection
                clickpt_v = event.GetPositionTuple()
                (clickpt_v_x, clickpt_v_y) = clickpt_v
                clickpt_t_x = (clickpt_v_x_self.view_offset_x)/self.tile_size_x
                clickpt_t_y = (clickpt_v_y_self.view_offset_y)/self.tile_size_y
                clickpt_g = self.tiles.Tile2Geo(clickpt_t_x, clickpt_t_y)

#                clickpt_m = self.tiles.ConvertView2Geo(clickpt_v,
#                                                       self.view_offset_x,
#                                                       self.view_offset_y,
#                                                       self.map_llon,
#                                                       self.map_tlat,
#                                                       self.ppd_x,
#                                                       self.ppd_y)

                # check each layer for a point select callback
                # we work on a copy as user callback could change order
                for id in self.layer_z_order[:]:
                    l = self.layer_mapping[id]
                    # if layer visible and selectable
                    if l.selectable and l.visible:
                        if l.map_rel:
                            p_data = self.layerPSelHandler[l.type](l, clickpt_g)
                        else:
                            p_data = self.layerPSelHandler[l.type](l, clickpt_v)
                        self.RaiseSelectEvent(EventPointSelect, l, p_data,
                                              vposn=clickpt_v,
                                              mposn=clickpt_g)

                        # user code possibly updated screen
                        delayed_paint = True

        # turn off drag
        self.was_dragging = False

        # turn off box selection mechanism
        self.is_box_select = False
        self.sbox_1_x = self.sbox_1_y = None

        # force PAINT event if required
        if delayed_paint:
            self.Update()

    def OnLeftDClick(self, event):
        """Left mouse button double-click.

        Zoom in (if possible).
        Zoom out (if possible) if shift key is down.
        """

        # ignore next Left UP event
        self.ignore_next_up = True

        # TODO: should ignore double-click off the map, but within view
        # a possible workaround is to limit minimum view level

        # get view coords of mouse double click, want same centre afterwards
        xy = event.GetPositionTuple()

        if event.ShiftDown():
            # zoom out if shift key also down
            if self.ZoomToLevel(self.level - 1):
                self.ZoomOut(xy)
        else:
            # zoom in
            if self.ZoomToLevel(self.level + 1):
                self.ZoomIn(xy)

    def OnMiddleDown(self, event):
        """Middle mouse button down.  Do nothing in this version."""

        pass

    def OnMiddleUp(self, event):
        """Middle mouse button up.  Do nothing in this version."""

        pass

    def OnRightDown(self, event):
        """Right mouse button down. Prepare for right select (no drag)."""

        click_posn = event.GetPositionTuple()

        if event.ShiftDown():
            self.is_box_select = True
            self.SetCursor(wx.StockCursor(wx.CURSOR_CROSS))
            (self.sbox_w, self.sbox_h) = (0, 0)
            (self.sbox_1_x, self.sbox_1_y) = click_posn
        event.Skip()

    def OnRightUp(self, event):
        """Right mouse button up.

        Note that when we iterate through the layer_z_order list we must
        iterate on a *copy* as the user select process can modify
        self.layer_z_order.
        """

        if self.ignore_next_right_up:
            self.ignore_next_right_up = False
            return

        self.SetCursor(wx.StockCursor(wx.CURSOR_DEFAULT))

        # we need a repaint to remove any selection box, but NOT YET!
        delayed_paint = self.sbox_1_x       # True if box select active

        # if any layers interested, inform of possible select
        if self.is_box_select:
            # possible box selection
#            ll_corner_v = (self.sbox_1_x, self.sbox_1_y)
#            tr_corner_v = (self.sbox_1_x + self.sbox_w,
#                           self.sbox_1_y + self.sbox_h)

            ll_corner_x = (self.sbox_1_x+self.view_offset_x) / self.tile_size_x
            ll_corner_y = (self.sbox_1_y+self.view_offset_y) / self.tile_size_y

            ll_corner_g = self.tiles.Tile2Geo(ll_corner_x, ll_corner_y)
            tr_corner_g = self.tiles.Tile2Geo(ll_corner_x + self.sbox_w,
                                              ll_corner_y + self.sbox_h)

#            ll_corner_m = self.tiles.ConvertView2Geo(ll_corner_v,
#                                                     self.view_offset_x,
#                                                     self.view_offset_y,
#                                                     self.map_llon,
#                                                     self.map_tlat,
#                                                     self.ppd_x,
#                                                     self.ppd_y)
#            tr_corner_m = self.tiles.ConvertView2Geo(tr_corner_v,
#                                                     self.view_offset_x,
#                                                     self.view_offset_y,
#                                                     self.map_llon,
#                                                     self.map_tlat,
#                                                     self.ppd_x,
#                                                     self.ppd_y)

            # check each layer for a box select event
            # we work on a copy as user response could change order
            for id in self.layer_z_order[:]:
                l = self.layer_mapping[id]
                if l.selectable and l.visible:   # and l.event_box_select:
                    if l.map_rel:
                        # map-relative, get all points selected (if any)
                        pts = self.layerBSelHandler[l.type](l, ll_corner_g,
                                                            tr_corner_g)
                    else:
                        # view-relative
                        pts = self.layerBSelHandler[l.type](l, 
                                                            (ll_corner_x,
                                                             ll_corner_y),
                                                            (ll_corner_x+self.sbox_w,
                                                             ll_corner_y+self.sbox_h))
                    self.RaiseSelectEvent(EventRightBoxSelect, l, pts)

                    # user code possibly updated screen
                    delayed_paint = True
            self.is_box_select = False
        else:
            # possible point selection
            clickpt_v = event.GetPositionTuple()
            (clickpt_v_x, clickpt_v_y) = clickpt_v
            clickpt_v_x = clickpt_v_x + self.view_offset_x
            clickpt_v_y = clickpt_v_y + self.view_offset_y
            clickpt_m = self.tiles.Tile2Geo(clickpt_v_x, clickpt_v_y)
#            clickpt_m = self.tiles.ConvertView2Geo(clickpt_v,
#                                                   self.view_offset_x,
#                                                   self.view_offset_y,
#                                                   self.map_llon,
#                                                   self.map_tlat,
#                                                   self.ppd_x,
#                                                   self.ppd_y)
            # check each layer for a point select callback
            # we work on a copy as user callback could change order
            for id in self.layer_z_order[:]:
                l = self.layer_mapping[id]
                # if layer visible, selectable and there is a callback
                if l.selectable and l.visible:
                    if l.map_rel:
                        pt = self.layerPSelHandler[l.type](l, clickpt_m)
                    else:
                        pt = self.layerPSelHandler[l.type](l, clickpt_v)
                    self.RaiseSelectEvent(EventRightPointSelect, l, pt,
                                          mposn=clickpt_m,
                                          vposn=clickpt_v)

                    # user code possibly updated screen
                    delayed_paint = True

        # turn off box selection mechanism
        self.is_box_select = False
        self.sbox_1_x = self.sbox_1_y = None

        # force PAINT event to remove selection box (if required)
        if delayed_paint:
            self.Update()

    def OnRightDClick(self, event):
        """Right mouse button double-click."""

        # ignore next RIGHT UP event
        self.ignore_next_right_up = True

    def OnMouseWheel(self, event):
        """Mouse wheel event."""

        # get centre of view in map coords, want same centre afterwards
        x = self.view_width / 2
        y = self.view_height / 2

        # determine which way to zoom, & *can* we zoom?
        if event.GetWheelRotation() > 0:
            if self.ZoomToLevel(self.level + 1):
                self.ZoomIn((x, y))
        else:
            if self.ZoomToLevel(self.level - 1):
                self.ZoomOut((x, y))

    ######
    # Method that overrides _BufferedCanvas.Draw() method.
    # This code does the actual drawing of tiles, layers, etc.
    ######

    def Draw(self, dc):
        """Do actual map tile and layers drawing.
        Overrides the _BufferedCanvas.draw() method.

        dc  device context to draw on

        The idea is to create 4 things that define the tiles to be drawn and
        where to draw them:
            x_pix_start  view pixel coord of left side of top-left tile
            y_pix_start  view pixel coord of top side of top-left tole
            row_list     list (top -> bottom) of tile rows
            col_list     list (left -> right) of tile columns

        Note that (x_pix_start, y_pix_start) will typically be OUTSIDE the view
        if the view is smaller than the map.
        """

        # figure out how to draw tiles
        if self.view_offset_x < 0:
            # View > Map in X - centre in X direction
            col_list = range(0, self.tiles.num_tiles_x)
            x_pix_start = -self.view_offset_x
        else:
            # Map > View - determine layout in X direction
            x_offset = self.view_offset_x + self.move_dx
            start_x_tile = int(x_offset / self.tile_size_x)
            stop_x_tile = ((x_offset + self.view_width + self.tile_size_x - 1)
                           / self.tile_size_x)
            stop_x_tile = int(stop_x_tile)
            col_list = range(start_x_tile, stop_x_tile)
            x_pix_start = start_x_tile * self.tile_size_y - x_offset

        if self.view_offset_y < 0:
            # View > Map in Y - centre in Y direction
            row_list = range(0, self.tiles.num_tiles_y)
            y_pix_start = -self.view_offset_y
        else:
            y_offset = self.view_offset_y + self.move_dy
            start_y_tile = int(y_offset / self.tile_size_y)
            stop_y_tile = ((y_offset + self.view_height
                            + self.tile_size_y - 1) / self.tile_size_y)
            stop_y_tile = int(stop_y_tile)
            row_list = range(start_y_tile, stop_y_tile)
            y_pix_start = start_y_tile * self.tile_size_y - y_offset

        # start pasting tiles onto the view
        # use x_pix and y_pix to place tiles
        log('Draw: col_list=%s' % str(col_list))
        log('Draw: row_list=%s' % str(row_list))
        x_pix = x_pix_start
        for x in col_list:
            y_pix = y_pix_start
            for y in row_list:
                tile = self.tiles.GetTile(x, y)
                dc.DrawBitmap(tile, x_pix, y_pix, False)
                y_pix += self.tile_size_y
            x_pix += self.tile_size_x

        # draw layers
        for id in self.layer_z_order:
            l = self.layer_mapping[id]
            if l.visible and self.level in l.show_levels:
                l.painter(dc, l.data, map_rel=l.map_rel)

        # draw selection rectangle, if any
        if self.sbox_1_x:
            penclr = wx.Colour(0, 0, 255)
            dc.SetPen(wx.Pen(penclr, width=1))
            brushclr = wx.Colour(0, 0, 0)
            dc.SetBrush(wx.Brush(brushclr, style=wx.TRANSPARENT))
            dc.DrawRectangle(self.sbox_1_x, self.sbox_1_y,
                             self.sbox_w, self.sbox_h)

    ######
    # Miscellaneous
    ######

    def ResizeCallback(self, event=None):
        """Handle a window resize.

        event  that caused the resize, may be None (not used)

        Handle all possible states of view and map:
           . new view entirely within map
           . map smaller than view (just centre map)

        Set up view state.
        """

        # get new size of the view
        (self.view_width, self.view_height) = self.GetClientSizeTuple()

        # if map > view in X axis
        if self.map_width > self.view_width:
            self.max_x_offset = self.map_width - self.view_width
            # do nothing unless background is showing
            # if map left edge right of view edge
            if self.view_offset_x < 0:
                # move view to hide background at left
                self.view_offset_x = 0
            elif self.view_offset_x + self.view_width > self.map_width:
                # move view to hide background at right
                self.view_offset_x = self.map_width - self.view_width
        else:
            # else view >= map - centre map in X direction
            self.max_x_offset = self.map_width - self.view_width
            self.view_offset_x = self.max_x_offset / 2

        # if map > view in Y axis
        if self.map_height > self.view_height:
            self.max_y_offset = self.map_height - self.view_height
            # do nothing unless background is showing
            # if map top edge below view edge
            if self.view_offset_y < 0:
                # move view to hide background at top
                self.view_offset_y = 0
            elif self.view_offset_y + self.view_height > self.map_height:
                # move view to hide background at bottom
                self.view_offset_y = self.map_height - self.view_height
        else:
            # else view >= map - centre map in Y direction
            self.max_y_offset = self.map_height - self.view_height
            self.view_offset_y = self.max_y_offset / 2

        # set the left/right/top/bottom lon/lat extents
        self.RecalcViewLonLatLimits()

    def RecalcViewLonLatLimits(self):
        """Recalculate the view geo extent values.

        Assumes only:
            self.view_offset_x
            self.view_offset_y
            self.view_width
            self.view_height
        values have been set.  All are map pixel values.
        """

        log('RecalcViewLonLatLimits: self.ppd_x=%.3f, self.ppd_y=%.3f' % (self.ppd_x, self.ppd_y))
        log('RecalcViewLonLatLimits: self.view_offset_x=%s, self.view_offset_y=%s' % (str(self.view_offset_x), str(self.view_offset_y)))
        log('RecalcViewLonLatLimits: self.map_llon=%s, self.map_tlat=%s' % (str(self.map_llon), str(self.map_tlat)))

#        (self.view_llon, self.view_tlat) = \
#                self.tiles.ConvertView2Geo((0, 0),
#                                           self.view_offset_x,
#                                           self.view_offset_y,
#                                           self.map_llon,
#                                           self.map_tlat,
#                                           self.ppd_x,
#                                           self.ppd_y)
        tltile_x = self.view_offset_x/self.tiles.tile_size_x
        tltile_y = self.view_offset_y/self.tiles.tile_size_y
        (self.view_tlat, self.view_llon) = self.tiles.Tile2Geo(tltile_x,
                                                               tltile_y)
        log('tltile_x=%s, tltile_y=%s' % (str(tltile_x), str(tltile_y)))
        log('self.view_tlat=%f, self.view_llon=%f' % (self.view_tlat, self.view_llon))

#        (self.view_rlon, self.view_blat) = \
#                self.tiles.ConvertView2Geo((self.view_width, self.view_height),
#                                           self.view_offset_x,
#                                           self.view_offset_y,
#                                           self.map_llon,
#                                           self.map_tlat,
#                                           self.ppd_x,
#                                           self.ppd_y)
        (self.view_blat, self.view_rlon) = self.tiles.Tile2Geo(tltile_x + 1.0,
                                                               tltile_y + 1.0)
        log('tltile_x+1.0=%s, tltile_y+1.0=%s' % (str(tltile_x+1.0), str(tltile_y+1.0)))
        log('self.view_blat=%f, self.view_rlon=%f' % (self.view_blat, self.view_rlon))

        log('RecalcViewLonLatLimits: finished')

    def ZoomToLevel(self, level):
        """Use a new tile level.

        level  the new tile level to use.

        Returns True if all went well.
        Maintain centre of map, if possible.
        """

        log('ZoomToLevel: level=%d' % level)

        if self.min_level <= level <= self.max_level:
            self.tiles.UseLevel(level)
            self.level = level
            self.map_width = self.tiles.num_tiles_x * self.tiles.tile_size_x
            self.map_height = self.tiles.num_tiles_y * self.tiles.tile_size_y
#            (self.map_width, self.map_height,
#                 self.ppd_x, self.ppd_y) = map_extent
            (self.map_llon, self.map_rlon,
                    self.map_blat, self.map_tlat) = self.tiles.extent

            # raise level change event
            self.RaiseLevelChangeEvent(level)

            log('ZoomToLevel: self.map_tlat=%.3f, self.map_blat=%.3f' % (self.map_tlat, self.map_blat))
            log('ZoomToLevel: self.ppd_x=%.3f, self.ppd_y=%.3f' % (self.ppd_x, self.ppd_y))

            return True

        return False

    def GetMapCoordsFromView(self, posn):
        """Convert view pixel coordinates to map coordinates.

        posn  is a tuple (x, y) of view pixel coordinates

        Returns (x, y) map pixel coordinates.
        """

        # unpack the position
        (view_x, view_y) = posn

        # calculate map coords
        map_x = view_x + self.view_offset_x
        map_y = view_y + self.view_offset_y

        return (map_x, map_y)

    ######
    # Select helpers - get objects that were selected
    ######

    def GetNearestPointInLayer(self, layer, pt):
        """Determine if clicked location selects a point in layer data.

        layer  layer object we are looking in
        pt     click geo location (lon, lat) or screen (x, y)

        Return None (no selection) or ((x, y), data) of closest point.
        """

# TODO: speed this up?  Do we need to??
# http://en.wikipedia.org/wiki/Kd-tree
# would need to create kd-tree in AddLayer()

        (ptx, pty) = pt
        res = None
        dist = 9999999.0        # more than possible
        if layer.map_rel:
            for p in layer.data:
                (x, y, _, _, _, _, _, data) = p
                d = (x - ptx) * (x - ptx) + (y - pty) * (y - pty)
                if d < dist:
                    dist = d
                    res = ((x, y), data)

            if dist <= layer.delta:
                return res
        else:
            for p in layer.data:
                dc = wx.BufferedPaintDC(self, self.buffer)
                (dc_w, dc_h) = dc.GetSize()
                dc_w2 = dc_w / 2
                dc_h2 = dc_h / 2
                dc_h -= 1
                dc_w -= 1
                (x, y, place, _, _, x_off, y_off, pdata) = p
                exec self.point_view_placement[place]
                d = (x - ptx) * (x - ptx) + (y - pty) * (y - pty)
                if d < dist:
                    dist = d
                    res = ((x, y), pdata)

            if dist <= layer.delta:
                return res

        return None

    def GetBoxSelPointsInLayer(self, layer, p1, p2):
        """Get list of points inside box.

        layer  reference to layer object we are working on
        p1     one corner point of selection box (tile coords)
        p2     opposite corner point of selection box (tile coords)

        We have to figure out which corner is which.

        Return a list of (lon, lat) of points inside box.
        Return None (no selection) or list [((lon, lat), data), ...]
        of points inside the selection box.
        """

# TODO: speed this up?  Do we need to??
        # get canonical box limits
        (p1x, p1y) = p1
        (p2x, p2y) = p2
        lx = min(p1x, p2x)      # left x coord
        rx = max(p1x, p2x)
        ty = max(p1y, p2y)      # top y coord
        by = min(p1y, p2y)

        # get a list of points inside the selection box
        result = []

        if layer.map_rel:
            for p in layer.data:
                (x, y, _, _, _, _, _, pdata) = p
                if lx <= x <= rx and by <= y <= ty:
                    result.append(((x, y), pdata))
        else:
            for p in layer.data:
                dc = wx.BufferedPaintDC(self, self.buffer)
                (dc_w, dc_h) = dc.GetSize()
                dc_w2 = dc_w / 2
                dc_h2 = dc_h / 2
                dc_h -= 1
                dc_w -= 1
                (x, y, place, _, _, x_off, y_off, pdata) = p
                exec self.point_view_placement[place]
                if lx <= x <= rx and by <= y <= ty:
                    result.append(((x, y), pdata))

        return result

    def GetNearestImageInLayer(self, layer, pt):
        """Decide if clicked location selects an image object in layer data.

        layer  layer object we are looking in
        pt     click geo location (lon, lat)

        Return None (no selection) or data for closest image.
        """

        (ptx, pty) = pt
        res = None
        dist = 9999999.0        # more than possible
        for p in layer.data:
            x = p[0]
            y = p[1]
            d = (x - ptx) * (x - ptx) + (y - pty) * (y - pty)
            if d < dist:
                dist = d
                res = (x, y)

        if dist <= layer.delta:
            return res

        return None

    def GetBoxSelImagesInLayer(self, layer, p1, p2):
        """Get list of images inside box p1-p2.

        layer  reference to layer object we are working on
        p1     one corner point of selection box (tile coords)
        p2     opposite corner point of selection box (tile coords)

        We have to figure out which corner is which.

        Return a list of (lon, lat) of points inside box.
        """

        # get canonical box limits
        (p1x, p1y) = p1
        (p2x, p2y) = p2
        lx = min(p1x, p2x)      # left x coord
        rx = max(p1x, p2x)
        ty = max(p1y, p2y)      # top y coord
        by = min(p1y, p2y)

        result = []
        for p in layer.data:
            x = p[0]
            y = p[1]
            if lx <= x <= rx and by <= y <= ty:
                result.append((x, y))

        return result

    def GetNearestPolygonInLayer(self, layer, pt):
        """Get nearest polygon object in layer data.

        layer  layer object we are looking in
        pt     click geo location (lon, lat)

        Return None (no selection) or data for closest polygon.

        Code here originally supplied by Stefan Harwarth, from
        [http://paulbourke.net/geometry/insidepoly/].
        """

        (ptx, pty) = pt

        for poly in layer.data:
            p = poly[0]
            if point_inside_polygon(ptx, pty, p):
                return p           

        return None

    def GetBoxSelPolygonsInLayer(self, layer, p1, p2):
        """Get list of polygons inside box p1-p2.

        layer  reference to layer object we are working on
        p1     one corner point of selection box (tile coords)
        p2     opposite corner point of selection box (tile coords)

        We have to figure out which corner is which.

        Return a list of (lon, lat) of points inside box.
        """

        return []

    def GetNearestTextInLayer(self, layer, pt):
        """Determine if clicked location selects a text object in layer data.

        layer  layer object we are looking in
        pt     click geo location (lon, lat)

        Return None (no selection) or data for closest text.

        Just search for text 'hotspot' - just like point select.
        Later make text sensitive (need text extent data).
        """

        (ptx, pty) = pt
        res = None
        dist = 1.0E+100        # more than possible
        for p in layer.data:
            (x, y, _, _, _, _, _, _, _, _, _, data) = p
            d = (x - ptx) * (x - ptx) + (y - pty) * (y - pty)
            if d < dist:
                dist = d
                res = ((x, y), data)

        if dist <= layer.delta:
            return res

        return None

    def GetBoxSelTextsInLayer(self, layer, p1, p2):
        """Get list of text objects inside box p1-p2.

        layer  reference to layer object we are working on
        p1     one corner point of selection box (tile coords)
        p2     opposite corner point of selection box (tile coords)

        We have to figure out which corner is which.

        Return a list of (lon, lat) of points inside box.
        Return None (no selection) or list [((lon, lat), data), ...]
        of points inside the selection box.
        """

        # get canonical box limits
        (p1x, p1y) = p1
        (p2x, p2y) = p2
        lx = min(p1x, p2x)      # left x coord
        rx = max(p1x, p2x)
        ty = max(p1y, p2y)      # top y coord
        by = min(p1y, p2y)

        # get a list of points inside the selection box
        result = []

        for p in layer.data:
            (x, y, _, _, _, _, _, _, _, _, _, data) = p
            if lx <= x <= rx and by <= y <= ty:
                result.append(((x, y), data))

        return result

    ######
    # The next two routines could be folded into one as they are the same.
    # However, if we ever implement a 'staged' zoom, we need both routines.
    ######

    def ZoomIn(self, xy):
        """Zoom map in to the next level.

        xy  is a tuple (x, y) of pixel coords of new centre after zoom

        The tile stuff has already been set to the correct level.
        """

        # set view state
        (map_x, map_y) = self.GetMapCoordsFromView(xy)
        self.view_offset_x = map_x * 2 - self.view_width / 2
        self.view_offset_y = map_y * 2 - self.view_height / 2

        # set some internal state through size code
        self.ResizeCallback()

        self.Update()

    def ZoomOut(self, xy):
        """Zoom map out to the previous level.

        xy  is a tuple (x, y) of pixel coords of new centre after zoom

        The tile stuff has already been set to the correct level.
        """

        # set view state
        (map_x, map_y) = self.GetMapCoordsFromView(xy)
        self.view_offset_x = map_x / 2 - self.view_width / 2
        self.view_offset_y = map_y / 2 - self.view_height / 2

        # set some internal state through size code
        self.ResizeCallback()

        self.Update()

    ######
    # Routines for pySlip events
    ######

    # there is no set_select_event() method and no self.select_event boolean
    # flag as the user controls selectability on a layer-by-layer basis.

    def RaiseSelectEvent(self, evtype, layer, point, mposn=None, vposn=None):
        """Raise a point SELECT event.

        evtype  select event type
        layer   layer the select was on
        point   point(s) selected, ie, a single or list of point tuples:
                    ((x, y), data)
        mposn   map coordinates of the mouse click
        vposn   view coordinates of the mouse click

        Note: this could be a BOX select, ie, multiple points in 'point'.
        """

        event = _PySlipEvent(_myEVT_PYSLIP_SELECT, self.GetId())
        event.evtype = evtype
        event.layer_id = layer.id
        event.point = point
        event.mposn = mposn
        event.vposn = vposn
        self.GetEventHandler().ProcessEvent(event)

    def SetLevelChangeEvent(self, event):
        """Set event routine on level change.

        event  True if event is to be raised on change
        """

        self.change_level_event = event

    def RaiseLevelChangeEvent(self, level):
        """Raise a LEVEL event."""

        if self.change_level_event:
            event = _PySlipEvent(_myEVT_PYSLIP_LEVEL, self.GetId())
            event.level = level
            self.GetEventHandler().ProcessEvent(event)

    def SetMousePositionEvent(self, event):
        """Set callback function on mouse move.

        event  True if event is to be raised on mouse move
        """

        self.mouse_position_event = event

    def RaiseMousePositionEvent(self, posn):
        """Raise a mouse position event.

        posn  the new mouse position (in view pixel coordinates)
        """

        if self.mouse_position_event:
            event = _PySlipEvent(_myEVT_PYSLIP_POSITION, self.GetId())
            log('RaiseMousePositionEvent: posn=%s, event=%s' % (str(posn), str(event)))
            if posn and self.PositionIsOnMap(posn):
                (posn_x, posn_y) = posn
                tile_x = float(self.view_offset_x + posn_x)/self.tile_size_x
                tile_y = float(self.view_offset_y + posn_y)/self.tile_size_y
                log('tile_x=%s, tile_y=%s' % (str(tile_x), str(tile_y)))
                event.position = self.tiles.Tile2Geo(tile_x, tile_y)
#                event.position = self.tiles.ConvertView2Geo(posn,
#                                                            self.view_offset_x,
#                                                            self.view_offset_y,
#                                                            self.map_llon,
#                                                            self.map_tlat,
#                                                            self.ppd_x,
#                                                            self.ppd_y)
            else:
                event.position = None
            self.GetEventHandler().ProcessEvent(event)

    def PositionIsOnMap(self, posn):
        """Return True if 'posn' is actually on map (not just view).

        posn  a tuple (x,y) position in view pixel coordinates
        """

        (x, y) = posn

        if self.view_offset_x < 0:
            if x < -self.view_offset_x:
                return False
            if x > self.view_width + self.view_offset_x:
                return False

        if self.view_offset_y < 0:
            if y < -self.view_offset_y:
                return False
            if y > self.view_height + self.view_offset_y:
                return False

        return True

    def get_i18n_kw(self, kwargs, kws, default):
        """Get alternate international keyword value.

        kwargs   dictionary to look for keyword value
        kws      iterable of keyword spelling strings
        default  default value if no keyword found

        Returns the keyword value.
        """

        result = None
        for kw_str in kws[:-1]:
            result = kwargs.get(kw_str, None)
            if result:
                break
        else:
            result = kwargs.get(kws[-1], default)

        return result
