'''
Copyright (C) 2020 CG Cookie
http://cgcookie.com
hello@cgcookie.com

Created by Jonathan Denning, Jonathan Williamson

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import os
import re
import math
import time
import struct
import random
import traceback
import functools
import urllib.request
from itertools import chain, zip_longest
from concurrent.futures import ThreadPoolExecutor

import bpy
import bgl
from bpy.types import BoolProperty
from mathutils import Matrix

from .parse import Parse_CharStream, Parse_Lexer
from .ui_utilities import (
    convert_token_to_string, convert_token_to_cursor,
    convert_token_to_color, convert_token_to_numberunit,
    get_converter_to_string,
    skip_token,
)

from .decorators import blender_version_wrapper, debug_test_call, add_cache
from .maths import Point2D, Vec2D, clamp, mid, Color, NumberUnit
from .profiler import profiler
from .drawing import Drawing, ScissorStack
from .utils import iter_head
from .shaders import Shader
from .fontmanager import FontManager



'''

CookieCutter UI Styling

This styling file is formatted _very_ similarly to CSS, but below is a list of a few important notes/differences:

- rules are applied top-down, so any later conflicting rule will override an earlier rule
    - in other words, specificity is ignored here (https://developer.mozilla.org/en-US/docs/Web/CSS/Specificity)
    - if you want to override a setting, place it lower in the styling input.
    - there is no `!important` keyword

- all units are in pixels; do not specify units (ex: `px`, `in`, `em`, `%`)
    - TODO: change to allow for %?

- colors can come in various formats
    - `rgb(<r>,<g>,<b>)` or `rgba(<r>,<g>,<b>,<a>)`, where r,g,b values in 0--255; a in 0.0--1.0
    - `hsl(<h>,<s>%,<l>%)` or `hsla(<h>,<s>%,<l>%,<a>)`, where h in 0--360; s,l in 0--100 (%); a in 0.0--1.0
    - `#RRGGBB`, where r,g,b in 00--FF
    - or by colorname

- selectors
    - all element types must be explicitly specified, except at beginning or when following a `>`; use `*` to match any type
        - ex: `elem1 .class` is the same as `elem1.class` and `elem1 . class`, but never `elem1 *.class`
    - only `>` and ` ` combinators are implemented

- spaces,tabs,newlines are completely ignored except to separate tokens

- numbers cannot begin with a decimal. instead, start with `0.` (ex: use `0.014` not `.014`)

- background has only color (no images)
    - `background: <background-color>`

- border has no style (such as dotted or dashed) and has uniform width (no left, right, top, bottom widths)
    - `border: <border-width> <border-color>`

- setting `width` or `height` will set both of the corresponding `min-*` and `max-*` properties

- `min-*` and `max-*` are used as suggestions to the UI system; they will not be strictly followed


Things to think about:

- `:scrolling` pseudoclass, for when we're scrolling through content
- `:focus` pseudoclass, for when textbox has focus, or changing a number input
- add drop shadow (draws in the margins?) and outline (for focus viz)
- allow for absolute, fixed, relative positioning?
- allow for float boxes?
- z-index (how is this done?  nodes of render tree get placed in rendering list?)
- ability to be drag-able?


'''

token_attribute = r'\[(?P<key>[-a-zA-Z_]+)((?P<op>=)"(?P<val>[^"]*)")?\]'

token_rules = [
    ('ignore', skip_token, [
        r'[ \t\r\n]',           # ignoring any tab, space, newline
        r'/[*][\s\S]*?[*]/',    # multi-line comments
    ]),
    ('special', convert_token_to_string, [
        r'[-.*>{},();#~]|[:]+',
    ]),
    ('combinator', convert_token_to_string, [
        r'[>~]',
    ]),
    ('attribute', convert_token_to_string, [
        token_attribute,
    ]),
    ('key', convert_token_to_string, [
        r'color',
        r'display',
        r'background(-(color|image))?',
        r'margin(-(left|right|top|bottom))?',
        r'padding(-(left|right|top|bottom))?',
        r'border(-(width|radius))?',
        r'border(-(left|right|top|bottom))?-color',
        r'((min|max)-)?width',
        r'((min|max)-)?height',
        r'left|top|right|bottom',
        r'cursor',
        r'overflow(-x|-y)?',
        r'position',
        r'flex(-(direction|wrap|grow|shrink|basis))?',
        r'justify-content|align-content|align-items',
        r'font(-(style|weight|size|family))?',
        r'white-space',
        r'content',
        r'object-fit',
        r'text-shadow',
        r'z-index',
    ]),
    ('value', convert_token_to_string, [
        r'auto',
        r'inline|block|none|flexbox|table(-row|-cell)?',    # display
        r'visible|hidden|scroll|auto',                      # overflow, overflow-x, overflow-y
        r'static|absolute|relative|fixed|sticky',           # position
        r'column|row',                                      # flex-direction
        r'nowrap|wrap',                                     # flex-wrap
        r'flex-start|flex-end|center|stretch',              # justify-content, align-content, align-items
        r'normal|italic',                                   # font-style
        r'normal|bold',                                     # font-weight
        r'serif|sans-serif|monospace',                      # font-family
        r'normal|nowrap|pre|pre-wrap|pre-line',             # white-space
        r'normal|none',                                     # content (more in url and string below)
        r'fill|contain|cover|none|scale-down',              # object-fit
        r'none',                                            # text-shadow
    ]),
    ('url', get_converter_to_string('url'), [
        r'url\([\'"]?(?P<url>[^)]*?)[\'"]?\)',
    ]),
    ('string', get_converter_to_string('string'), [
        r'"(?P<string>[^"]*?)"',
    ]),
    ('cursor', convert_token_to_cursor, [
        r'default|auto|initial',
        r'none|wait|grab|crosshair|pointer',
        r'text',
        r'e-resize|w-resize|ew-resize',
        r'n-resize|s-resize|ns-resize',
        r'all-scroll',
    ]),
    ('color', convert_token_to_color, [
        r'rgb\( *(?P<red>\d+) *, *(?P<green>\d+) *, *(?P<blue>\d+) *\)',
        r'rgba\( *(?P<red>\d+) *, *(?P<green>\d+) *, *(?P<blue>\d+) *, *(?P<alpha>\d+(\.\d+)?) *\)',
        r'hsl\( *(?P<hue>\d+) *, *(?P<saturation>\d+)% *, *(?P<lightness>\d+)% *\)',
        r'hsla\( *(?P<hue>\d+([.]\d*)?) *, *(?P<saturation>\d+([.]\d*)?)% *, *(?P<lightness>\d+([.]\d*)?)% *, *(?P<alpha>\d+([.]\d+)?) *\)',
        r'#[a-fA-F0-9]{6}',

        r'transparent',

        # https://www.quackit.com/css/css_color_codes.cfm
        r'indianred|lightcoral|salmon|darksalmon|lightsalmon|crimson|red|firebrick|darkred',        # reds
        r'pink|lightpink|hotpink|deeppink|mediumvioletred|palevioletred',                           # pinks
        r'coral|tomato|orangered|darkorange|orange',                                                # oranges
        r'gold|yellow|lightyellow|lemonchiffon|lightgoldenrodyellow|papayawhip|moccasin',           # yellows
        r'peachpuff|palegoldenrod|khaki|darkkhaki',                                                 #   ^
        r'lavender|thistle|plum|violet|orchid|fuchsia|magenta|mediumorchid|mediumpurple',           # purples
        r'blueviolet|darkviolet|darkorchid|darkmagenta|purple|rebeccapurple|indigo',                #   ^
        r'mediumslateblue|slateblue|darkslateblue',                                                 #   ^
        r'greenyellow|chartreuse|lawngreen|lime|limegreen|palegreen|lightgreen',                    # greens
        r'mediumspringgreen|springgreen|mediumseagreen|seagreen|forestgreen|green',                 #   ^
        r'darkgreen|yellowgreen|olivedrab|olive|darkolivegreen|mediumaquamarine',                   #   ^
        r'darkseagreen|lightseagreen|darkcyan|teal',                                                #   ^
        r'aqua|cyan|lightcyan|paleturquoise|aquamarine|turquoise|mediumturquoise',                  # blues
        r'darkturquoise|cadetblue|steelblue|lightsteelblue|powderblue|lightblue|skyblue',           #   ^
        r'lightskyblue|deepskyblue|dodgerblue|cornflowerblue|royalblue|blue|mediumblue',            #   ^
        r'darkblue|navy|midnightblue',                                                              #   ^
        r'cornsilk|blanchedalmond|bisque|navajowhite|wheat|burlywood|tan|rosybrown',                # browns
        r'sandybrown|goldenrod|darkgoldenrod|peru|chocolate|saddlebrown|sienna|brown|maroon',       #   ^
        r'white|snow|honeydew|mintcream|azure|aliceblue|ghostwhite|whitesmoke|seashell',            # whites
        r'beige|oldlace|floralwhite|ivory|antiquewhite|linen|lavenderblush|mistyrose',              #   ^
        r'gainsboro|lightgray|lightgrey|silver|darkgray|darkgrey|gray|grey|dimgray|dimgrey',        # grays
        r'lightslategray|lightslategrey|slategray|slategrey|darkslategray|darkslategrey|black',     #   ^
    ]),
    ('pseudoclass', convert_token_to_string, [
        r'hover',   # applies when mouse is hovering over
        r'active',  # applies between mousedown and mouseup
        r'focus',   # applies if element has focus
        r'disabled',    # applies if element is disabled
        # r'link',    # unvisited link
        # r'visited', # visited link
    ]),
    ('pseudoelement', convert_token_to_string, [
        r'before',  # inserts content before element
        r'after',   # inserts content after element
        # r'first-letter',
        # r'first-line',
        # r'selection',
    ]),
    ('num', convert_token_to_numberunit, [
        r'(?P<num>-?((\d*[.]\d+)|\d+))(?P<unit>px|vw|vh|pt|%|)',
    ]),
    ('id', convert_token_to_string, [
        r'[a-zA-Z_][a-zA-Z_\-0-9]*',
    ]),
]


default_fonts = {
    'default':       ('normal', 'normal', '12', 'sans-serif'),
    'caption':       ('normal', 'normal', '12', 'sans-serif'),
    'icon':          ('normal', 'normal', '12', 'sans-serif'),
    'menu':          ('normal', 'normal', '12', 'sans-serif'),
    'message-box':   ('normal', 'normal', '12', 'sans-serif'),
    'small-caption': ('normal', 'normal', '12', 'sans-serif'),
    'status-bar':    ('normal', 'normal', '12', 'sans-serif'),
}

default_styling = {
    'background': convert_token_to_color('transparent'),
    'display': 'inline',
}


# (?P<type>[^\n .#:[=\]]+)(?:(?:\.(?P<class>[^\n .#:[=\]]+))|(?:::(?P<pseudoelement>[^\n .#:[=\]]+))|(?::(?P<pseudoclass>[^\n .#:[=\]]+))|(?:#(?P<id>[^\n .#:[=\]]+))|(?:\[(?P<akey>[^\n .#:[=\]]+)(?:=\"(?P<aval>[^\"]+)\")?\]))*
# (?:(?P<type>[ .#:[]+)(?P<name>[^\n .#:[=\]]+)(?:=\"(?P<val>[^\"]+)\")?]?)

class UI_Style_Declaration:
    '''
    CSS Declarations are of the form:

        property: value;
        property: val0 val1 ...;

    Value is either a single token or a tuple if the token immediately following the first value is not ';'.

        ex: border: 1 yellow;

    '''

    def from_lexer(lexer):
        prop = lexer.match_t_v('key')
        lexer.match_v_v(':')
        v = lexer.next_v();
        if lexer.peek_v() == ';':
            val = v
        else:
            # tuple!
            l = [v]
            while lexer.peek_v() not in {';', '}'}:
                l.append(lexer.next_v())
            val = tuple(l)
        lexer.match_v_v(';')
        return UI_Style_Declaration(prop, val)

    def __init__(self, prop="", val=""):
        self.property = prop
        self.value = val
    def __str__(self):
        return '<UI_Style_Declaration "%s=%s">' % (self.property, str(self.value))
    def __repr__(self): return self.__str__()


class UI_Style_RuleSet:
    '''
    CSS RuleSets are in the form shown below.
    Note: each `property: value;` is a UI_Style_Declaration

        selector {
            property0: value;
            property1: val0 val1 val2;
            ...
        }

    '''

    @staticmethod
    def from_lexer(lexer):
        rs = UI_Style_RuleSet()

        def match_identifier():
            if lexer.peek_v() in {'.','#',':','::'}:
                e = '*'
            elif lexer.peek_v() == '*':
                e = lexer.match_v_v('*')
            else:
                e = lexer.match_t_v('id')
            while True:
                if lexer.peek_v() in {'.','#'}:
                    e += lexer.match_v_v({'.','#'})
                    e += lexer.match_t_v('id')
                elif lexer.peek_v() == ':':
                    e += lexer.match_v_v(':')
                    e += lexer.match_t_v('pseudoclass')
                elif lexer.peek_v() == '::':
                    e += lexer.match_v_v('::')
                    e += lexer.match_t_v('pseudoelement')
                elif 'attribute' in lexer.peek_t():
                    e += lexer.match_t_v('attribute')
                else:
                    break
            return e

        # get selector
        rs.selectors = [[]]
        while lexer.peek_v() != '{':
            if lexer.peek_v() == '*' or 'id' in lexer.peek_t():
                rs.selectors[-1].append(match_identifier())
            elif 'combinator' in lexer.peek_t():
                # TODO: handle + and ~ combinators?
                combinator = lexer.match_t_v('combinator')
                rs.selectors[-1].append(combinator)
                rs.selectors[-1].append(match_identifier())
            elif lexer.peek_v() == ',':
                lexer.match_v_v(',')
                rs.selectors.append([])
            else:
                assert False, 'expected selector or "{" but saw "%s" on line %d' % (lexer.peek_v(),lexer.current_line())

        # get declarations list
        rs.decllist = []
        lexer.match_v_v('{')
        while lexer.peek_v() != '}':
            while lexer.peek_v() == ';': lexer.match_v_v(';')
            if lexer.peek_v() == '}': break
            rs.decllist.append(UI_Style_Declaration.from_lexer(lexer))
        lexer.match_v_v('}')

        return rs

    @staticmethod
    def from_decllist(decllist, selector): # tagname, pseudoclass=None):
        # t = type(pseudoclass)
        # if t is list or t is set: pseudoclass = ':'.join(pseudoclass)
        rs = UI_Style_RuleSet()
        # rs.selectors = [[tagname + (':%s'%pseudoclass if pseudoclass else '')]]
        rs.selectors = [selector]
        for k,v in decllist.items():
            rs.decllist.append(UI_Style_Declaration(k,v))
        return rs

    def __init__(self):
        self.selectors = []
        self.decllist = []
        self._match_cache = {}

    def __str__(self):
        s = ', '.join(' '.join(selector) for selector in self.selectors)
        if not self.decllist: return '<UI_Style_RuleSet "%s">' % (s,)
        return '<UI_Style_RuleSet "%s"\n%s\n>' % (s,'\n'.join('  '+l for d in self.decllist for l in str(d).splitlines()))
    def __repr__(self): return self.__str__()

    @staticmethod
    @add_cache('_cache', {})
    def _split_selector(sel):
        # TODO: rewrite to use regex iterator only
        cache = UI_Style_RuleSet._split_selector._cache
        osel = str(sel)
        if osel not in cache:
            p = {'type':'', 'class':[], 'id':'', 'pseudoelement':[], 'pseudoclass':[], 'attribs':set(), 'attribvals':{}}
            transition = {'.':'class', '#':'id', '::':'pseudoelement', ':':'pseudoclass'}
            for attrib in re.finditer(token_attribute, sel):
                k,v = attrib.group('key'),attrib.group('val')
                if v is None: p['attribs'].add(k)
                else: p['attribvals'][k] = v
            sel = re.sub(token_attribute, '', sel)
            sel = re.sub(r'([.]|#|::|:|\[|\])', r' \1 ', sel).split(' ')
            v,m = '','type'
            for c in sel:
                if c in {'.',':','::','#'}:
                    if type(p[m]) is list: p[m].append(v)
                    else: p[m] = v
                    v,m = '',transition[c]
                else:
                    v += c
            if type(p[m]) is list: p[m].append(v)
            else: p[m] = v
            cache[osel] = p
        return cache[osel]

    @staticmethod
    @add_cache('_cache', {})
    def _match_selector(sel_elem, sel_style, cont):
        # ex:
        #   sel_elem  = ['body:hover', 'button:hover']
        #   sel_style = ['button:hover']
        if not sel_style: return True       # nothing left to match (potential extra in element)
        if not sel_elem: return False       # nothing left to match, but still have extra in style
        msel = UI_Style_RuleSet._match_selector
        cache = msel._cache
        key = '%s %s %s' % (str(sel_elem), str(sel_style), str(cont))
        if key not in cache:
            a0,b0 = sel_elem[-1],sel_style[-1]
            if b0 == '>':
                # parent selector in style MUST match (> means child, not descendant)
                return msel(sel_elem, sel_style[:-1], False)
            ap = UI_Style_RuleSet._split_selector(a0)
            bp = UI_Style_RuleSet._split_selector(b0)
            def matches():
                # NOTE: ap['type'] == '' with UI_Elements that contain the innertext
                # TODO: consider giving this a special type, ex: **text**
                return all([
                    ((bp['type'] == '*' and ap['type'] != '') or ap['type'] == bp['type']),
                    (bp['id'] == '' or ap['id'] == bp['id']),
                    all(c in ap['class'] for c in bp['class']),
                    all(c in ap['pseudoelement'] for c in bp['pseudoelement']),
                    all(c in ap['pseudoclass'] for c in bp['pseudoclass']),
                    all(key in ap['attribs'] for key in bp['attribs']),
                    all(key in ap['attribvals'] and ap['attribvals'][key] == val for (key,val) in bp['attribvals'].items()),
                ])
            if matches() and msel(sel_elem[:-1], sel_style[:-1], True): r = True
            elif not cont: r = False
            else: r = msel(sel_elem[:-1], sel_style, True)
            cache[key] = r
        return cache[key]

    @staticmethod
    def match_selector(sel_elem, sel_style):
        # must match the final element, so pass False for cont
        return UI_Style_RuleSet._match_selector(sel_elem, sel_style, False)

    @profiler.function
    def match(self, selector):
        # returns true if passed selector matches any selector in self.selectors
        key = tuple(selector)
        if key not in self._match_cache:
            self._match_cache[key] = any(UI_Style_RuleSet.match_selector(selector, sel) for sel in self.selectors)
        return self._match_cache[key]

    def get_all_matches(self, selector):
        return [sel for sel in self.selectors if UI_Style_RuleSet.match_selector(selector, sel)]


class UI_Styling:
    '''
    Parses input to a CSSOM-like object
    '''
    uid = 0

    @staticmethod
    @profiler.function
    def from_var(var, tagname='*', pseudoclass=None):
        if not var: return UI_Styling()
        if type(var) is UI_Styling: return var
        sel = tagname + (':%s' % pseudoclass if pseudoclass else '')
        if type(var) is dict: var = ['%s:%s' % (k,v) for (k,v) in var.items()]
        if type(var) is list: var = ';'.join(var)
        if type(var) is str:  var = UI_Styling('%s{%s;}' % (sel,var))
        assert type(var) is UI_Styling
        return var

    @staticmethod
    @profiler.function
    def from_file(filename):
        lines = open(filename, 'rt').read()
        return UI_Styling(lines)

    def load_from_file(self, filename):
        text = open(filename, 'rt').read()
        self.load_from_text(text)

    @profiler.function
    def load_from_text(self, text):
        self.clear_cache()
        self.rules = []
        if not text: return
        charstream = Parse_CharStream(text)             # convert input into character stream
        lexer = Parse_Lexer(charstream, token_rules)    # tokenize the character stream
        while lexer.peek_t() != 'eof':
            self.rules.append(UI_Style_RuleSet.from_lexer(lexer))
        # print('UI_Styling.load_from_text: Loaded %d rules' % len(self.rules))

    def clear_cache(self):
        # print('UI_Styling%d.clear_cache' % self._uid)
        self._decllist_cache = {}

    @staticmethod
    def from_decllist(decllist, selector=None, var=None): # tagname='*', pseudoclass=None):
        if selector is None: selector = ['*']
        if var is None: var = UI_Styling()
        var.rules = [UI_Style_RuleSet.from_decllist(decllist, selector)]
        # var.rules = [UI_Style_RuleSet.from_decllist(decllist, tagname, pseudoclass)]
        return var

    def __init__(self, lines=''):
        self._uid = UI_Styling.uid
        UI_Styling.uid += 1
        self.rules = []
        self._decllist_cache = {}
        self._matches_cache = {}
        if lines: self.load_from_text(lines)

    def __str__(self):
        if not self.rules: return '<UI_Styling%d>' % self._uid
        return '<UI_Styling%d\n%s\n>' % (self._uid, '\n'.join('  '+l for r in self.rules for l in str(r).splitlines()))

    def __repr__(self): return self.__str__()

    @property
    def simple_str(self): return '<UI_Styling%d>' % self._uid

    @profiler.function
    def get_decllist(self, selector):
        if not self.rules: return []
        selector_key = tuple(selector) #'~'.join(selector)
        if selector_key not in self._decllist_cache:
            with profiler.code('UI_Styling.get_decllist: creating cached value'):
                self._decllist_cache[selector_key] = [d for rule in self.rules if rule.match(selector) for d in rule.decllist]
                # print('UI_Styling.get_decllist', self._uid, '%d/%d' % (len(self._decllist_cache[selector_key]), len(self.rules)), selector_key)
        return self._decllist_cache[selector_key]

    def _has_matches(self, selector):
        if not self.rules: return False
        selector_key = tuple(selector)
        if selector_key not in self._matches_cache:
            self._matches_cache[selector_key] = any(rule.match(selector) for rule in self.rules)
        return self._matches_cache[selector_key]

    def get_all_stylings(self, selector):
        return [sel for rule in self.rules for sel in rule.get_all_matches(selector)]

    def append(self, other_styling):
        self.clear_cache()
        self.rules += other_styling.rules
        return self


    @staticmethod
    def _trbl_split(v):
        # NOTE: if v is a tuple, either: (scalar, unit) or ((scalar, unit), (scalar, unit), ...)
        # TODO: IGNORING UNITS??
        if type(v) is not tuple: return (v, v, v, v)
        l = len(v)
        if l == 1: return (v[0], v[0], v[0], v[0])
        if l == 2: return (v[0], v[1], v[0], v[1])
        if l == 3: return (v[0], v[1], v[2], v[1])
        return (v[0], v[1], v[2], v[3])

    @staticmethod
    def _font_split(vs):
        if type(vs) is not tuple:
            return default_fonts[vs] if vs in default_fonts else default_fonts['default']
        return tuple(v if v else d for (v,d) in zip_longest(vs,default_fonts['default']))

    @staticmethod
    @profiler.function
    def _expand_declarations(decls):
        decllist = {}
        for decl in decls:
            p,v = decl.property, decl.value
            if p in {'margin','padding'}:
                vals = UI_Styling._trbl_split(v)
                decllist['%s-top'%p]    = vals[0]
                decllist['%s-right'%p]  = vals[1]
                decllist['%s-bottom'%p] = vals[2]
                decllist['%s-left'%p]   = vals[3]
            elif p == 'border':
                if type(v) is not tuple: v = (v,)
                if type(v[0]) is NumberUnit or type(v[0]) is float:
                    decllist['border-width'] = v[0]
                    v = v[1:]
                if v:
                    vals = UI_Styling._trbl_split(v)
                    decllist['border-top-color']    = vals[0]
                    decllist['border-right-color']  = vals[1]
                    decllist['border-bottom-color'] = vals[2]
                    decllist['border-left-color']   = vals[3]
            elif p == 'border-color':
                vals = UI_Styling._trbl_split(v)
                decllist['border-top-color']    = vals[0]
                decllist['border-right-color']  = vals[1]
                decllist['border-bottom-color'] = vals[2]
                decllist['border-left-color']   = vals[3]
            elif p == 'font':
                vals = UI_Styling._font_split(v)
                decllist['font-style'] = v[0]
                decllist['font-weight'] = v[1]
                decllist['font-size'] = v[2]
                decllist['font-family'] = v[3]
            elif p == 'background':
                if type(v) is not tuple: v = (v,)
                for ev in v:
                    if type(ev) is Color:
                        decllist['background-color'] = ev
                    else:
                        decllist['background-image'] = ev
            elif p == 'width':
                decllist['width'] = v
                # decllist['min-width'] = v
                # decllist['max-width'] = v
            elif p == 'height':
                decllist['height'] = v
                decllist['min-height'] = v
                decllist['max-height'] = v
            elif p == 'overflow':
                if v == 'scroll':
                    decllist['overflow-x'] = 'auto'
                    decllist['overflow-y'] = 'scroll'
                else:
                    decllist['overflow-x'] = v
                    decllist['overflow-y'] = v
            else:
                decllist[p] = v
        # filter out properties with `initial` values
        decllist = { k:v for (k,v) in decllist.items() if v != 'initial' }
        return decllist

    @staticmethod
    @profiler.function
    def compute_style(selector, *stylings):
        if selector is None: return {}
        full_decllist = [dl for styling in stylings if styling for dl in styling.get_decllist(selector)]
        decllist = UI_Styling._expand_declarations(full_decllist)
        return decllist

    @staticmethod
    def has_matches(selector, *stylings):
        if selector is None: return False
        return any(styling._has_matches(selector) for styling in stylings if styling)

    @profiler.function
    def filter_styling(self, selector):
        decllist = self.compute_style(selector, self)
        styling = UI_Styling.from_decllist(decllist, selector=selector)
        return styling


ui_defaultstylings = UI_Styling()
def load_defaultstylings():
    global ui_defaultstylings
    path = os.path.join(os.path.dirname(__file__), 'config', 'ui_defaultstyles.css')
    if os.path.exists(path): ui_defaultstylings.load_from_file(path)
    else: ui_defaultstylings.rules = []
load_defaultstylings()