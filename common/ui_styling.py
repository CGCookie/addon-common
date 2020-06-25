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
import copy
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
from .utils import iter_head, UniqueCounter
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
# (?:(?P<type>[.#:[]+)?(?P<name>[^\n .#:[=\]]+)(?:=\"(?P<val>[^\"]+)\")?]?)

selector_splitter = re.compile(r"(?:(?P<type>[.#:[]+)?(?P<name>[^\n .#:\[=\]]+)(?:=\"(?P<val>[^\"]+)\")?\]?)")


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
    CSS RuleSets are in the form shown below, where there is a single list of selectors followed by block set of styling rules
    Note: each `property: value;` is a UI_Style_Declaration

        selector, selector {
            property0: value;
            property1: val0 val1 val2;
            ...
        }

    '''

    uid_generator = UniqueCounter()

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
        self._uid = UI_Style_RuleSet.uid_generator.next()
        self.selectors = []     # can have multiple selectors for same decllist
        self.decllist = []      # list of style declarations that apply
        self._match_cache = {}

    def __str__(self):
        s = ', '.join(' '.join(selector) for selector in self.selectors)
        if not self.decllist: return '<UI_Style_RuleSet "%s">' % (s,)
        return '<UI_Style_RuleSet "%s"\n%s\n>' % (s,'\n'.join('  '+l for d in self.decllist for l in str(d).splitlines()))
    def __repr__(self): return self.__str__()

    @staticmethod
    @add_cache('_cache', {})
    def _split_selector(sel):
        # (?:(?P<type>[.#:[]+)?(?P<name>[^\n .#:[=\]]+)(?:=\"(?P<val>[^\"]+)\")?]?)
        cache = UI_Style_RuleSet._split_selector._cache
        osel = str(sel)
        if osel not in cache:
            p = {'type':'', 'class':set(), 'id':'', 'pseudoelement':set(), 'pseudoclass':set(), 'attribs':set(), 'attribvals':{}}

            for part in selector_splitter.finditer(sel):
                t,n,v = part.group('type'),part.group('name'),part.group('val')
                if t is None:   p['type'] = n
                elif t == '.':  p['class'].add(n)
                elif t == '#':  p['id'] = n
                elif t == ':':  p['pseudoclass'].add(n)
                elif t == '::': p['pseudoelement'].add(n)
                elif t == '[':
                    if v is None: p['attribs'].add(n)
                    else: p['attribvals'][n] = v
                else: assert False, 'Unhandled selector type "%s" (%s, %s) in "%s"' % (str(t), str(n), str(v), str(sel))

            # p['names'] is a set of all identifying elements in selector
            # useful for quickly and conservatively deciding that selector does NOT match
            p['names'] = p['class'] | p['pseudoelement'] | p['pseudoclass'] | p['attribs'] | p['attribvals'].keys() # | p['attribvals'].values()
            if p['type'] not in {'*','>'}: p['names'].add(p['type'])
            if p['id']: p['names'].add(p['id'])

            cache[osel] = p
        return dict(cache[osel])  # NOTE: _not_ a deep copy!

    @staticmethod
    @add_cache('_cache', {})
    def _join_selector_parts(p):
        cache = UI_Style_RuleSet._join_selector_parts._cache
        op = str(p)
        if op not in cache:
            sel = p['type'] if p['type'] else '*'
            if p['class']:         sel += ''.join('.%s' % c for c in p['class'])
            if p['id']:            sel += '#%s' % p['id']
            if p['pseudoclass']:   sel += ''.join(':%s' % pc for pc in p['pseudoclass'])
            if p['pseudoelement']: sel += ''.join(':%s' % pe for pe in p['pseudoelement'])
            if p['attribs']:       sel += ''.join('[%s]' % a for a in p['attribs'])
            if p['attribvals']:    sel += ''.join('[%s="%s"]' % (k,v) for (k,v) in p['attribvals'].items())
            cache[op] = sel
        return cache[op]

    @staticmethod
    def _match_selector_approx(parts_elem, parts_style, check_end=False):
        if check_end:
            if parts_style[-1]['type'] not in {'*','>'} and parts_elem[-1]['type'] != parts_style[-1]['type']:
                return False
            if parts_style[-1]['id'] and parts_elem[-1]['id'] != parts_style[-1]['id']:
                return False
        names_elem  = {n for p in parts_elem  for n in p['names']}
        names_style = {n for p in parts_style for n in p['names']} - {'*', '>'}
        if not all(n in names_elem for n in names_style): return False
        return True

    @staticmethod
    def _match_selector_parts(ap, bp):
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

    @staticmethod
    @profiler.function
    def _match_selector(sel_elem, pts_elem, sel_style, pts_style, cont=False):
        '''
        sel_elem/pts_elem and sel_style/pts_style are corresponding lists for element and style
            sel_*: selector     pts_*: selector broken into parts
        cont:
            if False, end of sel_elem/pts_elem and sel_style/pts_style must be exactly the same
            if True, can allow skipping end of sel_style/pts_style
        '''
        # ex:
        #   sel_elem = ['body:hover', 'button:hover']
        #   sel_style = ['button:hover']
        if not sel_style: return True   # nothing left to match (potential extra in element)
        if not sel_elem:  return False  # nothing left to match, but still have extra in style
        msel = UI_Style_RuleSet._match_selector
        mparts = UI_Style_RuleSet._match_selector_parts
        if sel_style[-1] == '>':
            # parent selector in style MUST match (> means child, not descendant)
            return msel(sel_elem, pts_elem, sel_style[:-1], pts_style[:-1])
        elif not UI_Style_RuleSet._match_selector_approx(pts_elem, pts_style, check_end=not cont):
            return False
        elif mparts(pts_elem[-1], pts_style[-1]) and msel(sel_elem[:-1], pts_elem[:-1], sel_style[:-1], pts_style[:-1], True):
            return True
        elif not cont:
            return False
        else:
            return msel(sel_elem[:-1], pts_elem[:-1], sel_style, pts_style, True)

    @staticmethod
    def match_selector(sel_elem, sel_style, strip=None):
        split = UI_Style_RuleSet._split_selector
        # print('UI_Style_RuleSet', sel_elem, sel_style)
        sel_elem  = UI_Styling.strip_selector_parts(sel_elem, strip)
        sel_style = UI_Styling.strip_selector_parts(sel_style, strip)
        parts_elem  = [split(p) for p in sel_elem]
        parts_style = [split(p) for p in sel_style]
        return UI_Style_RuleSet._match_selector(sel_elem, parts_elem, sel_style, parts_style)

    @profiler.function
    def match(self, sel_elem, strip=None):
        # returns true if passed selector matches any selector in self.selectors
        cache = self._match_cache
        key = f'{sel_elem} {strip}'
        if key not in cache:
            cache[key] = any(UI_Style_RuleSet.match_selector(sel_elem, sel_style, strip=strip) for sel_style in self.selectors)
        return cache[key]

    def get_all_matches(self, sel_elem):
        return [sel_style for sel_style in self.selectors if UI_Style_RuleSet.match_selector(sel_elem, sel_style)]

    @staticmethod
    @add_cache('_cache', {})
    def selector_specificity(selector, uid, inline=False):
        k = f'{selector} {inline}'
        cache = UI_Style_RuleSet.selector_specificity._cache
        if k not in cache:
            split = UI_Style_RuleSet._split_selector
            a = 1 if inline else 0  # inline
            b = 0                   # id
            c = 0                   # class, pseudoclass, attrib, attribval
            d = 0                   # type, pseudoelement
            e = uid                 # uid (used for ordering)
            parts = [split(sel) for sel in selector]
            for part in parts:
                b += 1 if part['id'] else 0
                c += len(part['class']) + len(part['pseudoclass']) + len(part['attribs']) + len(part['attribvals'])
                if part['type'] not in {'', '*', '>'}: d += 1
            cache[k] = (a, b, c, d, e)
        return cache[k]


class UI_Styling:
    '''
    Parses input to a CSSOM-like object
    '''
    uid_generator = UniqueCounter()

    @staticmethod
    @profiler.function
    def from_var(var, tagname='*', pseudoclass=None, inline=False):
        if not var: return UI_Styling(inline=inline)
        if type(var) is UI_Styling: return var
        sel = tagname + (':%s' % pseudoclass if pseudoclass else '')
        # NOTE: do not convert below into `t = type(var)` and change `if`s below into `elif`s!
        if type(var) is dict: var = ['%s:%s' % (k,v) for (k,v) in var.items()]
        if type(var) is list: var = ';'.join(var)
        if type(var) is str:  var = UI_Styling(lines=f'{sel}{{{var};}}', inline=inline)
        assert type(var) is UI_Styling
        return var

    @staticmethod
    @profiler.function
    def from_file(filename, inline=False):
        lines = open(filename, 'rt').read()
        return UI_Styling(lines=lines, inline=inline)

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
        UI_Styling.trim_styling._cache = {}
        UI_Styling.strip_selector_parts._cache = {}


    def _print_trie_to_node(self, node):
        # find path from node to root
        path = set()
        node_root = node
        while True:
            path.add(node_root['__uid'])
            if node_root['__parent'] is None: break
            node_root = node_root['__parent']
        print(path)
        def p(node_cur, depth):
            for k in node_cur:
                k2 = str(k).replace('"', '\\"')
                spc = "  " * depth
                if   k == '__rulesets':
                    print(f'{spc}"{k2}":["... ({len(node_cur[k])})"],')
                elif k == '__selectors':
                    v = str(node_cur[k]).replace('"', '\\"')
                    print(f'{spc}"{k2}":"{v}",')
                elif k == '__parent':
                    pass
                elif k == '__uid':
                    print(f'{spc}"__uid":{node_cur[k]},')
                else:
                    node_next = node_cur[k]
                    if node_next['__uid'] in path:
                        print(f'{spc}"{k2}":{{')
                        p(node_next, depth+1)
                        print(f'{spc}}},')
                    else:
                        print(f'{spc}"{k2}":{{ }},')
        print('{')
        p(node_root, 1)
        print('}')

    def _print_trie(self):
        def p(node_cur, depth):
            for k in node_cur:
                k2 = str(k).replace('"', '\\"')
                spc = "  " * depth
                if   k == '__rulesets':
                    print(f'{spc}"{k2}":["... ({len(node_cur[k])})"],')
                elif k == '__selectors':
                    v = str(node_cur[k]).replace('"', '\\"')
                    print(f'{spc}"{k2}":"{v}",')
                elif k == '__parent':
                    pass
                elif k == '__uid':
                    pass
                else:
                    print(f'{spc}"{k2}":{{')
                    p(node_cur[k], depth+1)
                    print(f'{spc}}},')
        print('{')
        p(self._trie, 1)
        print('}')

    def optimize(self):
        '''
        build a trie of selectors for faster matching
        the trie consists of
            selector parts: type (str, t), class (set, .c), id (str, #i), pseudoelement (set, ::pe), pseudoclass (set, :pc), attribs (set, [k]), attribvals (dict, [k=v])
            and >
        '''

        if self._trie: return  # already optimized!

        split = UI_Style_RuleSet._split_selector
        node_uid_generator = UniqueCounter()
        def new_node(node_parent):
            return {'__parent':node_parent, '__uid': node_uid_generator.next()}
        def get_node(cur, key):
            if key not in cur: cur[key] = new_node(cur)
            return cur[key]
        self._trie = new_node(None)

        # insert all items into trie
        for rule in self.rules:
            for selector in rule.selectors:
                specificity = UI_Style_RuleSet.selector_specificity(selector, rule._uid, inline=self._inline)
                # print(f'selector specificity: {selector} => {specificity}')
                parts = [split(p) for p in selector]
                part = {'type':'', 'id':'', 'class': set(), 'pseudoelement':set(), 'pseudoclass':set(), 'attribs':set(), 'attribvals':dict()}
                node_cur = self._trie
                while True:
                    if part['type']:
                        # NOTE: type can be '>', but this _should_ get handled in final `else`
                        assert part['type'] != '>', f'type can be `>` but not here. check if style has `> >`\nselector: {selector}\npart: {part}\nparts: {parts}\n{self._trie}'
                        node_cur = get_node(node_cur, f"{part['type']}")
                        part['type'] = ''
                    elif part['id']:
                        node_cur = get_node(node_cur, f"#{part['id']}")
                        part['id'] = ''
                    elif part['class']:
                        c = part['class'].pop()
                        node_cur = get_node(node_cur, f".{c}")
                    elif part['pseudoelement']:
                        pe = part['pseudoelement'].pop()
                        node_cur = get_node(node_cur, f"::{pe}")
                    elif part['pseudoclass']:
                        pc = part['pseudoclass'].pop()
                        node_cur = get_node(node_cur, f":{pc}")
                    elif part['attribs']:
                        a = part['attribs'].pop()
                        node_cur = get_node(node_cur, f"[{a}]")
                    elif part['attribvals']:
                        k,v = part['attribvals'].popitem()
                        node_cur = get_node(node_cur, f'[{k}="{v}"]')
                    elif not parts:
                        break
                    else:
                        skip = 1
                        if node_cur != self._trie:
                            if parts[-1]['type'] == '>':
                                node_cur = get_node(node_cur, '>')
                                skip = 2
                            else:
                                node_cur = get_node(node_cur, ' ')
                        part, parts = copy.deepcopy(parts[-skip]), parts[:-skip]
                node_cur.setdefault('__rulesets', list()).append((specificity, rule))
                node_cur.setdefault('__selectors', list()).append(selector)

        # print_trie()
        # print(sum(len(rule.selectors) for rule in self.rules), len(self._trie))

    def get_matching_rules(self, selector):
        self.optimize()
        rules = []
        def m(node_cur, part, parts, depth):
            nonlocal rules
            for (edge_label, node_next) in node_cur.items():
                if   edge_label == ' ':
                    ps = parts
                    while ps:
                        p,ps = ps[-1],ps[:-1]
                        m(node_next, p, ps, depth+1)
                elif edge_label == '>': m(node_next, parts[-1], parts[:-1], depth+1)
                elif edge_label == '*': m(node_next, part, parts, depth+1)
                elif edge_label[0] == '#':
                    if edge_label[1:] == part['id']: m(node_next, part, parts, depth+1)
                elif edge_label[0] == '.':
                    if edge_label[1:] in part['class']: m(node_next, part, parts, depth+1)
                elif len(edge_label) > 2 and edge_label[1] == ':':
                    if edge_label[2:] in part['pseudoelement']: m(node_next, part, parts, depth+1)
                elif edge_label[0] == ':':
                    if edge_label[1:] in part['pseudoclass']: m(node_next, part, parts, depth+1)
                elif edge_label[0] == '[':
                    attrib_parts = edge_label[1:-1].split('=')      # remove square brackets and split on `=`
                    attrib_key = attrib_parts[0]
                    if len(attrib_parts) == 1:
                        if attrib_key in part['attribs']: m(node_next, part, parts, depth+1)
                    else:
                        attrib_val = attrib_parts[1][1:-1]          # remove quotes from attribute value
                        if part['attribvals'].get(attrib_key) == attrib_val: m(node_next, part, parts, depth+1)
                elif edge_label in {'__selectors', '__parent', '__uid'}:
                    pass
                elif edge_label == '__rulesets':
                    rules.extend(node_cur['__rulesets'])
                else:
                    # assuming type
                    if edge_label == part['type']: m(node_next, part, parts, depth+1)
        split = UI_Style_RuleSet._split_selector
        parts = [split(p) for p in selector]
        m(self._trie, parts[-1], parts[:-1], 0)
        rules.sort(key=lambda sr:sr[0])
        return [r for (s,r) in rules]


    @staticmethod
    def from_decllist(decllist, selector=None, var=None, inline=False):
        if selector is None: selector = ['*']
        if var is None: var = UI_Styling(inline=inline)
        var.rules = [UI_Style_RuleSet.from_decllist(decllist, selector)]
        return var

    @staticmethod
    def from_selector_decllist_list(l, inline=False):
        var = UI_Styling(inline=inline)
        var.rules = [UI_Style_RuleSet.from_decllist(decllist, selector) for (selector,decllist) in l]
        return var

    def __init__(self, lines=None, inline=False):
        self._uid = UI_Styling.uid_generator.next()
        self._trie = None
        self._inline = inline
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
        cache = self._decllist_cache
        if not self.rules: return []
        oselector = str(selector)
        if oselector not in cache:
            # print('UI_Styling.get_decllist', selector)
            with profiler.code('UI_Styling.get_decllist: creating cached value'):
                decllist = [d for rule in self.get_matching_rules(selector) for d in rule.decllist]
                # decllist = [d for rule in self.rules if rule.match(selector) for d in rule.decllist]
                cache[oselector] = decllist
                # print('UI_Styling.get_decllist', self._uid, '%d/%d' % (len(cache[selector_key]), len(self.rules)), selector_key)
        return cache[oselector]

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
                decllist['font-style']  = vals[0]
                decllist['font-weight'] = vals[1]
                decllist['font-size']   = vals[2]
                decllist['font-family'] = vals[3]
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
                decllist['height']     = v
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
    @add_cache('_cache', {})
    def strip_selector_parts(selector, strip):
        if not strip: return selector
        cache = UI_Styling.strip_selector_parts._cache
        oselector = str((selector, strip))
        if oselector not in cache:
            nselector = []
            strip_type            = 'type' in strip
            strip_id              = 'id' in strip
            strip_classes         = 'classes' in strip
            strip_pseudoelements  = 'pseudoelements' in strip
            strip_pseudoclasses   = 'pseudoclasses' in strip
            strip_attributes      = 'attributes' in strip
            strip_attributevalues = 'attributevalues' in strip
            for sel in selector:
                # p = {'type':'', 'class':set(), 'id':'', 'pseudoelement':set(), 'pseudoclass':set(), 'attribs':set(), 'attribvals':{}}
                p = UI_Style_RuleSet._split_selector(str(sel))
                if strip_type:            p['type'] = '*'
                if strip_id:              p['id'] = ''
                if strip_classes:         p['class'] = set()
                if strip_pseudoelements:  p['pseudoelement'] = set()
                if strip_pseudoclasses:   p['pseudoclass'] = set()
                if strip_attributes:      p['attribs'] = set()
                if strip_attributevalues: p['attribvals'] = dict()
                nselector.append(UI_Style_RuleSet._join_selector_parts(p))
            cache[oselector] = nselector
        return cache[oselector]

    @staticmethod
    @add_cache('_cache', {})
    def trim_styling(selector, *stylings):
        cache = UI_Styling.trim_styling._cache
        strip = {
            # 'type',
            # 'classes',
            # 'id',
            'pseudoelements',
            'pseudoclasses',
            'attributes',
            'attributevalues',
        }
        nselector = UI_Styling.strip_selector_parts(selector, strip)
        onselector = str(nselector)
        if onselector not in cache:
            nstyling = UI_Styling()
            # include only the rules that _might_ apply to selector (assumes some selector parts change but others do not)
            nstyling.rules = [rule for styling in stylings for rule in styling.rules if rule.match(nselector, strip=strip)]
            if False:
                # trimmed = [rule for styling in stylings for rule in styling.rules if not rule.match(nselector, strip=strip)]
                print('trim_styling', selector, nselector, nstyling) #, trimmed)
            cache[onselector] = nstyling
        return cache[onselector]

    @staticmethod
    def combine_styling(*stylings, inline=False):
        nstyling = UI_Styling(inline=inline)
        nstyling.rules = [rule for styling in stylings for rule in styling.rules]
        return nstyling

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