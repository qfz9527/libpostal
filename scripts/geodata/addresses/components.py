# -*- coding: utf-8 -*-
import copy
import itertools
import operator
import os
import pycountry
import random
import re
import six
import unicodedata
import yaml

# Russian/Ukrainian parsing and inflection
import pymorphy2
import pymorphy2_dicts_ru
import pymorphy2_dicts_uk

from collections import defaultdict, OrderedDict

from geodata.address_formatting.formatter import AddressFormatter

from geodata.address_expansions.abbreviations import abbreviate
from geodata.address_expansions.equivalence import equivalent
from geodata.address_expansions.gazetteers import *
from geodata.addresses.config import address_config
from geodata.addresses.dependencies import ComponentDependencies
from geodata.addresses.buildings import Building
from geodata.addresses.floors import Floor
from geodata.addresses.entrances import Entrance
from geodata.addresses.house_numbers import HouseNumber
from geodata.addresses.metro_stations import MetroStation
from geodata.addresses.numbering import *
from geodata.addresses.po_boxes import POBox
from geodata.addresses.postcodes import PostCode
from geodata.addresses.staircases import Staircase
from geodata.addresses.superblocks import Superblock
from geodata.addresses.units import Unit
from geodata.boundaries.names import boundary_names
from geodata.configs.utils import nested_get, recursive_merge, RESOURCES_DIR, alternative_probabilities
from geodata.coordinates.conversion import latlon_to_decimal
from geodata.countries.constants import Countries
from geodata.countries.names import *
from geodata.encoding import safe_encode
from geodata.graph.topsort import topsort
from geodata.i18n.unicode_properties import *
from geodata.language_id.disambiguation import *
from geodata.language_id.sample import sample_random_language
from geodata.math.floats import isclose
from geodata.math.sampling import cdf, weighted_choice
from geodata.names.normalization import name_affixes
from geodata.numbers.ordinals import ordinal_expressions
from geodata.numbers.spellout import numeric_expressions
from geodata.osm.components import osm_address_components
from geodata.places.config import place_config
from geodata.states.state_abbreviations import state_abbreviations
from geodata.text.normalize import *
from geodata.text.tokenize import tokenize, token_types
from geodata.text.utils import is_numeric


this_dir = os.path.realpath(os.path.dirname(__file__))

PARSER_DEFAULT_CONFIG = os.path.join(RESOURCES_DIR, 'parser', 'default.yaml')

JAPANESE_ROMAJI = 'ja_rm'
ENGLISH = 'en'
SPANISH = 'es'


def setup_component_dependencies(config):
    component_dependencies = {}

    default_deps = config.get('component_dependencies', {})

    country_components = default_deps.pop('exceptions', {})
    for c in list(country_components):
        conf = copy.deepcopy(default_deps)
        recursive_merge(conf, country_components[c])
        country_components[c] = conf
    country_components[None] = default_deps

    for country, country_deps in six.iteritems(country_components):
        graph = {k: c['dependencies'] for k, c in six.iteritems(country_deps)}
        graph.update({c: [] for c in AddressFormatter.address_formatter_fields if c not in graph})

        component_dependencies[country] = ComponentDependencies(graph)
    return component_dependencies


def setup_valid_scripts():
    chars = get_chars_by_script()
    all_scripts = build_master_scripts_list(chars)
    script_codes = get_script_codes(all_scripts)
    valid_scripts = set(all_scripts) - set([COMMON_SCRIPT, UNKNOWN_SCRIPT])
    valid_scripts |= set([code for code, script in six.iteritems(script_codes) if script not in valid_scripts])
    return set([s.lower() for s in valid_scripts])


class AddressComponents(object):
    '''
    This class, while it has a few dependencies, exposes a simple method
    for transforming geocoded input addresses (usually a lat/lon with either
    a name or house number + street name) into the sorts of examples used by
    libpostal's address parser. The dictionaries produced here can be fed
    directly to AddressFormatter.format_address to produce training examples.

    There are several steps in expanding an address including reverse geocoding
    to polygons, disambiguating which language the address uses, stripping standard
    prefixes like "London Borough of", pruning duplicates like "Antwerpen, Antwerpen, Antwerpen".

    Usage:
    >>> components = AddressComponents(osm_admin_rtree, country_rtree, neighborhoods_rtree, places_index)
    >>> components.expand({'name': 'Hackney Empire'}, 51.54559, -0.05567)

    Returns (results vary because of randomness):

    ({'city': u'London',
      'city_district': u'London Borough of Hackney',
      'country': 'UK',
      'name': 'Hackney Empire',
      'state': u'England',
      'state_district': u'Greater London'},
     u'gb',
     u'en')

    '''
    iso_alpha2_codes = set([c.alpha2.lower() for c in pycountry.countries])
    iso_alpha3_codes = set([c.alpha3.lower() for c in pycountry.countries])

    latin_alphabet_lower = set([unichr(c) for c in xrange(ord('a'), ord('z') + 1)])

    BOUNDARY_COMPONENTS = OrderedDict.fromkeys((
        AddressFormatter.SUBDIVISION,
        AddressFormatter.METRO_STATION,
        AddressFormatter.SUBURB,
        AddressFormatter.CITY_DISTRICT,
        AddressFormatter.CITY,
        AddressFormatter.ISLAND,
        AddressFormatter.STATE_DISTRICT,
        AddressFormatter.STATE,
        AddressFormatter.COUNTRY,
    ))

    LOCALITY_COMPONENTS = OrderedDict.fromkeys((
        AddressFormatter.SUBDIVISION,
        AddressFormatter.LOCALITY,
        AddressFormatter.METRO_STATION,
    ))

    NAME_COMPONENTS = {
        AddressFormatter.ATTENTION,
        AddressFormatter.CARE_OF,
        AddressFormatter.HOUSE,
    }

    ADDRESS_LEVEL_COMPONENTS = {
        AddressFormatter.ATTENTION,
        AddressFormatter.CARE_OF,
        AddressFormatter.HOUSE,
        AddressFormatter.NAMED_BUILDING,
        AddressFormatter.HOUSE_NUMBER,
        AddressFormatter.ROAD,
        AddressFormatter.INTERSECTION,
        AddressFormatter.BUILDING,
        AddressFormatter.ENTRANCE,
        AddressFormatter.STAIRCASE,
        AddressFormatter.LEVEL,
        AddressFormatter.UNIT,
    }

    ALL_OSM_NAME_KEYS = set(['name', 'name:simple',
                             'ISO3166-1:alpha2', 'ISO3166-1:alpha3',
                             'short_name', 'name:short', 'alt_name', 'official_name'])

    NULL_PHRASE = 'null'
    ALPHANUMERIC_PHRASE = 'alphanumeric'
    STANDALONE_PHRASE = 'standalone'

    IRELAND = 'ie'
    JAMAICA = 'jm'

    class zones:
        COMMERCIAL = 'commercial'
        RESIDENTIAL = 'residential'
        INDUSTRIAL = 'industrial'
        UNIVERSITY = 'university'

    language_code_aliases = {
        'zh_py': 'zh_pinyin'
    }

    slavic_morphology_analyzers = {
        'ru': pymorphy2.MorphAnalyzer(pymorphy2_dicts_ru.get_path(), lang='ru'),
        'uk': pymorphy2.MorphAnalyzer(pymorphy2_dicts_uk.get_path(), lang='uk'),
    }

    sub_building_component_class_map = {
        AddressFormatter.BUILDING: Building,
        AddressFormatter.ENTRANCE: Entrance,
        AddressFormatter.STAIRCASE: Staircase,
        AddressFormatter.LEVEL: Floor,
        AddressFormatter.UNIT: Unit,
    }

    numeric_phrase_regexes = {}

    config = yaml.load(open(PARSER_DEFAULT_CONFIG))
    component_dependencies = setup_component_dependencies(config)
    valid_scripts = setup_valid_scripts()
    # Non-admin component dropout
    address_level_dropout_probabilities = {k: v['probability'] for k, v in six.iteritems(config['dropout'])}

    def __init__(self, osm_admin_rtree, country_rtree, neighborhoods_rtree, places_index):
        self.osm_admin_rtree = osm_admin_rtree
        self.country_rtree = country_rtree
        self.neighborhoods_rtree = neighborhoods_rtree
        self.places_index = places_index

    @classmethod
    def address_level_dropout_order(cls, components, country):
        '''
        Address component dropout
        -------------------------

        To make the parser more robust to different kinds of input (not every address is fully
        specified, especially in a geocoder, on mobile, with autocomplete, etc.), we want to
        train the parser with many types of addresses.

        This will help the parser not become too reliant on component order, e.g. it won't think
        that the first token in a string is always the venue name simply because that was the case
        in the training data.

        This method returns a dropout ordering ensuring that if the components are dropped in order,
        each set will be valid. In the parser config (resources/parser/default.yaml), the dependencies
        for each address component are specified, e.g. "house_number" depends on "road", so it would
        be invalid to have an address that was simply a house number with no other information. The
        caller of this method may decide to drop all the components at once or one at a time, creating
        N training examples from a single address.

        Some components are also more likely to be dropped than others, so in the same config there are
        dropout probabilities for each.
        '''
        if not components:
            return []

        component_bitset = ComponentDependencies.component_bitset(components)

        deps = cls.component_dependencies.get(country, cls.component_dependencies[None])
        candidates = [c for c in reversed(deps.dependency_order) if c in components and c in cls.address_level_dropout_probabilities]
        retained = set(candidates)

        dropout_order = []

        for component in candidates:
            if component not in retained:
                continue

            if random.random() >= cls.address_level_dropout_probabilities.get(component, 0.0):
                continue
            bit_value = deps.component_bit_values.get(component, 0)
            candidate_bitset = component_bitset ^ bit_value

            if all(((candidate_bitset & deps[c]) for c in retained if c != component)) or not (component_bitset & deps[component]):
                dropout_order.append(component)
                component_bitset = candidate_bitset
                retained.remove(component)
        return dropout_order

    @classmethod
    def strip_keys(cls, value, ignore_keys):
        for key in ignore_keys:
            value.pop(key, None)

    def osm_reverse_geocoded_components(self, latitude, longitude):
        return self.osm_admin_rtree.point_in_poly(latitude, longitude, return_all=True)

    def country_reverse_geocoded_components(self, latitude, longitude):
        return self.country_rtree.point_in_poly(latitude, longitude, return_all=True)

    country_polygon_requited_keys = set([
        'ISO3166-1:alpha2',
        'ISO3166-2',
        'is_in:country_code',
        'admin_level',
        'type',
        'id',
    ])

    @classmethod
    def country_polygon_minimal_properties(cls, props):
        return {k: v for k, v in six.iteritems(props) if k in cls.country_polygon_requited_keys}

    @classmethod
    def osm_country_and_languages(cls, osm_components):
        country = None
        for c in osm_components:
            country = c.get('ISO3166-1:alpha2')
            if country:
                break
        else:
            # See if there's an ISO3166-2 code that matches
            # in case the country polygon is wacky
            for c in osm_components:
                admin1 = c.get('ISO3166-2')
                if admin1:
                    # If so, and if the country is valid, use that
                    admin1_prefix = admin1[:2]
                    if Countries.is_valid_country_code(admin1_prefix):
                        country = admin1_prefix
                        break
                    else:
                        country = None
                else:
                    is_in_country = c.get('is_in:country_code')
                    if Countries.is_valid_country_code(is_in_country):
                        country = is_in_country
                        break

        if country is None:
            return None, []

        country = country.lower()

        regional = None

        for c in osm_components:
            place_id = '{}:{}'.format(c.get('type', 'relation'), c.get('id', '0'))

            regional = get_regional_languages(country, 'osm', place_id)

            if regional:
                break

        languages = []
        if not regional:
            languages = get_country_languages(country).items()
        else:
            if not all(regional.values()):
                languages = get_country_languages(country)
                languages.update(regional)
                languages = languages.items()
            else:
                languages = regional.items()

            if not languages:
                languages = get_country_languages(country).items()

        default_languages = sorted(languages, key=operator.itemgetter(1), reverse=True)

        return country, default_languages

    @classmethod
    def osm_component_is_village(cls, component):
        return component.get('place', '').lower() in ('locality', 'village', 'hamlet')

    @classmethod
    def categorize_osm_component(cls, country, props, containing_components):

        containing_ids = [(c['type'], c['id']) for c in containing_components if 'type' in c and 'id' in c]

        return osm_address_components.component_from_properties(country, props, containing=containing_ids)

    @classmethod
    def categorized_osm_components(cls, country, osm_components):
        components = []
        for i, props in enumerate(osm_components):
            name = props.get('name')
            if not name:
                continue

            component = cls.categorize_osm_component(country, props, osm_components)

            if component is not None:
                components.append((props, component))

        return components

    @classmethod
    def address_language(cls, components, candidate_languages):
        '''
        Language
        --------

        If there's only one candidate language for a given country or region,
        return that language.

        In countries that speak multiple languages (Belgium, Hong Kong, Wales, the US
        in Spanish-speaking regions, etc.), we need at least a road name for disambiguation.

        If we can't identify a language, the address will be labeled "unk". If the street name
        itself contains phrases from > 1 language, the address will be labeled ambiguous.
        '''
        language = None

        if len(candidate_languages) == 1:
            language = candidate_languages[0][0]
        else:
            street = components.get(AddressFormatter.ROAD, None)

            if street is not None:
                language = disambiguate_language(street, candidate_languages)
            else:
                if has_non_latin_script(candidate_languages):
                    for component, value in six.iteritems(components):
                        language, script_langs = disambiguate_language_script(value, candidate_languages)
                        if language is not UNKNOWN_LANGUAGE:
                            break
                    else:
                        language = UNKNOWN_LANGUAGE
                else:
                    default_languages = [lang for lang, default in candidate_languages if default]
                    if len(default_languages) == 1:
                        language = default_languages[0]
                    else:
                        language = UNKNOWN_LANGUAGE

        return language

    @classmethod
    def pick_random_name_key(cls, props, component, suffix=''):
        '''
        Random name
        -----------

        Pick a name key from OSM
        '''
        raw_key = boundary_names.name_key(props, component)

        key = ''.join((raw_key, suffix)) if ':' not in raw_key else raw_key
        return key, raw_key

    @classmethod
    def is_multiscript_name(cls, name):
        name = safe_decode(name)
        script, script_len, is_ascii = get_string_script(name)
        return script_len < len(name)

    @classmethod
    def strip_outer_parens(cls, name):
        name = safe_decode(name)
        if len(name) > 0:
            first_char = name[0]
            last_char = name[-1]
            start_idx = 0
            end_idx = len(name)
            if unicodedata.category(first_char) == 'Ps':
                start_idx += 1
            if unicodedata.category(last_char) == 'Pe':
                end_idx -= 1

            return name[start_idx:end_idx]
        return name

    @classmethod
    def expand_multiscript_name(cls, name, keep_multiscript=True):
        normalized_names = OrderedDict()
        name = safe_decode(name)

        while len(name):
            script, script_len, is_ascii = get_string_script(name)
            if script_len == len(name):
                name_span = cls.strip_outer_parens(name.strip())
                if name_span:
                    normalized_names[name_span] = None
            elif script_len > 0:
                name_span = cls.strip_outer_parens(name[:script_len].strip())
                if name_span:
                    normalized_names[name_span] = None
            elif script_len == 0:
                break

            name = name[script_len:]
        return normalized_names

    @classmethod
    def all_names(cls, props, language, component=None, keys=ALL_OSM_NAME_KEYS, add_raw_keys=True, expand_multiscript=False):
        # Preserve uniqueness and order
        valid_names, _ = boundary_names.name_key_dist(props, component)
        names = OrderedDict()
        valid_names = set([k for k in valid_names if k in keys])

        for k, v in six.iteritems(props):
            valid_value = False
            if k in valid_names and add_raw_keys:
                valid_value = True
            elif ':' in k:
                if k == 'name:simple' and language == ENGLISH and k in keys:
                    valid_value = True
                k, qual = k.split(':', 1)
                if qual.lower() == language:
                    valid_value = True

            if valid_value:
                for v_sub in v.split(u';'):
                    if v_sub not in names:
                        names[v_sub] = None

        if expand_multiscript:
            name_keys = names.keys()
            for name in name_keys:
                normalized = cls.expand_multiscript_name(name)
                names.update([(k, None) for k in normalized.keys() if k not in names])

        return names.keys()

    @classmethod
    def place_names_and_components(cls, name, osm_components, country=None, languages=None):
        names = set()
        components = defaultdict(set)

        name_norm = six.u('').join([t for t, c in normalized_tokens(name, string_options=NORMALIZE_STRING_LOWERCASE,
                                                                    token_options=TOKEN_OPTIONS_DROP_PERIODS, whitespace=True)])
        for i, props in enumerate(osm_components):
            containing_ids = [(c['type'], c['id']) for c in osm_components[i + 1:] if 'type' in c and 'id' in c]

            component = osm_address_components.component_from_properties(country, props, containing=containing_ids)

            component_names = set([n.lower() for n in cls.all_names(props, languages or [] )])

            valid_component_names = set()
            for n in component_names:
                norm = six.u('').join([t for t, c in normalized_tokens(n, string_options=NORMALIZE_STRING_LOWERCASE,
                                       token_options=TOKEN_OPTIONS_DROP_PERIODS, whitespace=True)])

                if norm == name_norm:
                    continue

                valid_component_names.add(norm)

            names |= valid_component_names

            is_state = False

            if component is not None:
                for cn in component_names:
                    components[cn.lower()].add(component)

                if not is_state:
                    is_state = component == AddressFormatter.STATE

            if is_state:
                for state in component_names:
                    for language in languages:
                        abbreviations = state_abbreviations.get_all_abbreviations(country, language, state, default=None)
                        if abbreviations:
                            abbrev_names = [a.lower() for a in abbreviations]
                            names.update(abbrev_names)
                            for a in abbrev_names:
                                components[a].add(AddressFormatter.STATE)

        return names, components

    @classmethod
    def strip_components(cls, name, osm_components, country, languages):
        if not name or not osm_components:
            return name

        tokens = tokenize(name)

        tokens_lower = normalized_tokens(name, string_options=NORMALIZE_STRING_LOWERCASE,
                                         token_options=TOKEN_OPTIONS_DROP_PERIODS)

        names, components = cls.place_names_and_components(name, osm_components, country=country, languages=languages)

        phrase_filter = PhraseFilter([(n, '') for n in names])

        phrases = list(phrase_filter.filter(tokens_lower))

        stripped = []

        for is_phrase, tokens, value in phrases:
            if not is_phrase:
                t, c = tokens
                if stripped and c not in (token_types.IDEOGRAPHIC_CHAR, token_types.IDEOGRAPHIC_NUMBER):
                    stripped.append(u' ')
                if c not in token_types.PUNCTUATION_TOKEN_TYPES:
                    stripped.append(t)

        name = u''.join(stripped)

        return name

    parens_regex = re.compile('\(.*?\)')

    @classmethod
    def normalized_place_name(cls, name, tag, osm_components, country=None, languages=None, phrase_from_component=False):
        '''
        Multiple place names
        --------------------

        This is to help with things like  addr:city="New York NY" and cleanup other invalid user-specified boundary names
        '''

        tokens = tokenize(name)
        # Sometimes there are garbage tags like addr:city="?", etc.
        if not phrase_from_component and not any((c in token_types.WORD_TOKEN_TYPES for t, c in tokens)):
            return None

        tokens_lower = normalized_tokens(name, string_options=NORMALIZE_STRING_LOWERCASE,
                                         token_options=TOKEN_OPTIONS_DROP_PERIODS)

        names, components = cls.place_names_and_components(name, osm_components, country=country, languages=languages)

        phrase_filter = PhraseFilter([(n, '') for n in names])

        phrases = list(phrase_filter.filter(tokens_lower))

        num_phrases = 0
        total_tokens = 0
        current_phrase_start = 0
        current_phrase_len = 0
        current_phrase = []

        for is_phrase, phrase_tokens, value in phrases:
            if is_phrase:
                whitespace = not any((c in (token_types.IDEOGRAPHIC_CHAR, token_types.IDEOGRAPHIC_NUMBER) for t, c in phrase_tokens))
                join_phrase = six.u(' ') if whitespace else six.u('')

                if num_phrases > 0 and total_tokens > 0:
                    # Remove hanging comma, slash, etc.
                    last_token, last_class = tokens[total_tokens - 1]
                    if last_class in token_types.NON_ALPHANUMERIC_TOKEN_TYPES:
                        total_tokens -= 1
                    # Return phrase with original capitalization
                    return join_phrase.join([t for t, c in tokens[:total_tokens]])
                elif num_phrases == 0 and total_tokens > 0 and not phrase_from_component:
                    # We're only talking about addr:city tags, etc. so default to
                    # the reverse geocoded components (better names) if we encounter
                    # an unknown phrase followed by a containing boundary phrase.
                    return None

                current_phrase_start = total_tokens
                current_phrase_len = len(phrase_tokens)

                current_phrase_tokens = tokens_lower[current_phrase_start:current_phrase_start + current_phrase_len]

                current_phrase = join_phrase.join([t for t, c in current_phrase_tokens])
                # Handles cases like addr:city="Harlem" when Harlem is a neighborhood
                tags = components.get(current_phrase, set())
                if tags and tag not in tags and not phrase_from_component:
                    return None

                total_tokens += len(phrase_tokens)
                num_phrases += 1
            else:
                total_tokens += 1

        if cls.parens_regex.search(name):
            name = cls.parens_regex.sub(six.u(''), name).strip()

        # If the name contains a comma, stop and only use the phrase before the comma
        if ',' in name:
            return name.split(',', 1)[0].strip()

        return name

    @classmethod
    def normalize_place_names(cls, address_components, osm_components, country=None, languages=None, phrase_from_component=False):
        for key in list(address_components):
            name = address_components[key]
            if key in cls.BOUNDARY_COMPONENTS:
                name = cls.normalized_place_name(name, key, osm_components,
                                                 country=country, languages=languages,
                                                 phrase_from_component=phrase_from_component)

                if name is not None:
                    address_components[key] = name
                else:
                    address_components.pop(key)

    @classmethod
    def normalize_address_components(cls, components):
        address_components = {k: v for k, v in components.iteritems()
                              if k in cls.formatter.aliases}
        cls.formatter.aliases.replace(address_components)
        return address_components

    @classmethod
    def combine_fields(cls, address_components, language, country=None, generated=None):
        combo_config = address_config.get_property('components.combinations', language, country=country, default={})

        combos = []
        probs = {}

        if generated is None:
            generated = {}

        for combo in combo_config:
            components = OrderedDict.fromkeys(combo['components']).keys()

            if not all((is_numeric(address_components.get(c, generated.get(c))) or generated.get(c) for c in components)):
                if combo['probability'] == 1.0:
                    for c in components:
                        if c in address_components and c in generated:
                            address_components.pop(c, None)
                continue

            combos.append((len(components), combo))

        if not combos:
            return None

        for num_components, combo in combos:
            prob = combo['probability']
            if random.random() < prob:
                break
        else:
            return None

        components = OrderedDict.fromkeys(combo['components']).keys()

        values = []
        probs = []
        for s in combo['separators']:
            values.append(s['separator'])
            probs.append(s['probability'])

        probs = cdf(probs)
        separator = weighted_choice(values, probs)

        new_label = combo['label']
        new_component = []
        for c in components:
            component = address_components.pop(c, None)
            if component is None and c in generated:
                component = generated[c]
            elif component is None:
                continue
            if hasattr(component, '__iter__'):
                component = component[0]
            new_component.append(component)

        new_value = separator.join(new_component)

        address_components[new_label] = new_value
        return set(components)

    @classmethod
    def generated_type(cls, component, existing_components, language, country=None):
        component_config = address_config.get_property('components.{}'.format(component), language, country=country)
        if not component_config:
            return None

        prob_dist = component_config

        conditionals = component_config.get('conditional', [])

        if conditionals:
            for vals in conditionals:
                c = vals['component']
                if c in existing_components:
                    prob_dist = vals['probabilities']
                    break

        values = []
        probs = []
        for num_type in (cls.NULL_PHRASE, cls.ALPHANUMERIC_PHRASE, cls.STANDALONE_PHRASE):
            key = '{}_probability'.format(num_type)
            prob = prob_dist.get(key)
            if prob is not None:
                values.append(num_type)
                probs.append(prob)
            elif num_type in prob_dist:
                values.append(num_type)
                probs.append(1.0)
                break

        if not probs:
            return None

        probs = cdf(probs)
        num_type = weighted_choice(values, probs)

        if num_type == cls.NULL_PHRASE:
            return None
        else:
            return num_type

    @classmethod
    def get_component_phrase(cls, component_class, value, language, country=None):
        value = safe_decode(value)
        if not is_numeric(value) and not (value.isalpha() and len(value) == 1):
            return None

        value, extracted = cls.extract_field(value, component_class, language, country=country)
        if not cls.cleanup_value_post_extraction(value):
            return None

        phrase = component_class.phrase(value, language, country=country)
        if phrase != value:
            return phrase
        else:
            return None

    @classmethod
    def normalize_sub_building_components(cls, address_components, language, country=None):
        for component, component_class in six.iteritems(cls.sub_building_component_class_map):
            if component in address_components:
                val = address_components[component]
                new_val = cls.get_component_phrase(component_class, val, language, country)
                if new_val is not None:
                    address_components[component] = new_val

    @classmethod
    def cldr_country_name(cls, country_code, language):
        '''
        Country names
        -------------

        In OSM, addr:country is almost always an ISO-3166 alpha-2 country code.
        However, we'd like to expand these to include natural language forms
        of the country names we might be likely to encounter in a geocoder or
        handwritten address.

        These splits are somewhat arbitrary but could potentially be fit to data
        from OpenVenues or other sources on the usage of country name forms.

        If the address includes a country, the selection procedure proceeds as follows:

        1. With probability a, select the country name in the language of the address
           (determined above), or with the localized country name if the language is
           undtermined or ambiguous.

        2. With probability b(1-a), sample a language from the distribution of
           languages on the Internet and use the country's name in that language.

        3. This is implicit, but with probability (1-b)(1-a), keep the country code
        '''

        cldr_config = nested_get(cls.config, ('country', 'cldr'))

        alpha_2_iso_code_prob = float(cldr_config['iso_alpha_2_code_probability'])
        localized_name_prob = float(cldr_config['localized_name_probability'])
        iso_3166_name_prob = float(cldr_config['iso_3166_name_probability'])
        alpha_3_iso_code_prob = float(cldr_config['iso_alpha_3_code_probability'])

        localized, iso_3166, alpha3, alpha2 = values = range(4)
        probs = cdf([localized_name_prob, iso_3166_name_prob, alpha_3_iso_code_prob, alpha_2_iso_code_prob])
        value = weighted_choice(values, probs)

        country_name = country_code.upper()

        if language in (AMBIGUOUS_LANGUAGE, UNKNOWN_LANGUAGE):
            language = None

        if value == localized:
            country_name = country_names.localized_name(country_code, language) or country_names.localized_name(country_code) or country_name
        elif value == iso_3166:
            country_name = country_names.iso_3166_name(country_code)
        elif value == alpha3:
            country_name = country_names.alpha3_code(country_code) or country_name

        return country_name

    @classmethod
    def is_country_iso_code(cls, country):
        country = country.lower()
        return country in cls.iso_alpha2_codes or country in cls.iso_alpha3_codes

    @classmethod
    def replace_country_name(cls, address_components, country, language):
        address_country = address_components.get(AddressFormatter.COUNTRY)

        cldr_country_prob = float(nested_get(cls.config, ('country', 'cldr_country_probability')))
        replace_with_cldr_country_prob = float(nested_get(cls.config, ('country', 'replace_with_cldr_country_probability')))
        remove_iso_code_prob = float(nested_get(cls.config, ('country', 'remove_iso_code_probability')))

        is_iso_code = address_country and cls.is_country_iso_code(address_country)

        if (is_iso_code and random.random() < replace_with_cldr_country_prob) or random.random() < cldr_country_prob:
            address_country = cls.cldr_country_name(country, language)
            if address_country:
                address_components[AddressFormatter.COUNTRY] = address_country
        elif is_iso_code and random.random() < remove_iso_code_prob:
            address_components.pop(AddressFormatter.COUNTRY)

    @classmethod
    def non_local_language(cls):
        non_local_language_prob = float(nested_get(cls.config, ('languages', 'non_local_language_probability')))
        if random.random() < non_local_language_prob:
            return sample_random_language()
        return None

    @classmethod
    def state_name(cls, address_components, country, language, non_local_language=None, always_use_full_names=False):
        '''
        States
        ------

        Primarily for the US, Canada and Australia, OSM addr:state tags tend to use the abbreviated
        state name whereas we'd like to include both forms. With some probability, replace the abbreviated
        name with the unabbreviated one e.g. CA => California
        '''
        address_state = address_components.get(AddressFormatter.STATE)

        if address_state and country and not non_local_language:
            state_full_name = state_abbreviations.get_full_name(country, language, address_state)

            state_full_name_prob = float(nested_get(cls.config, ('state', 'full_name_probability')))

            if state_full_name and (always_use_full_names or random.random() < state_full_name_prob):
                address_state = state_full_name
        elif address_state and non_local_language:
            _ = address_components.pop(AddressFormatter.STATE, None)
            address_state = None
        return address_state

    @classmethod
    def pick_language_suffix(cls, osm_components, language, non_local_language, more_than_one_official_language):
        '''
        Language suffix
        ---------------

        This captures some variations in languages written with different scripts
        e.g. language=ja_rm is for Japanese Romaji.

        Pick a language suffix with probability proportional to how often the name is used
        in the reverse geocoded components. So if only 2/5 components have name:ja_rm listed
        but 5/5 have either name:ja or just plain name, we would pick standard Japanese (Kanji) 
        with probability .7143 (5/7) and Romaji with probability .2857 (2/7).
        '''
        # This captures name variations like "ja_rm" for Japanese Romaji, etc.
        language_scripts = defaultdict(int)
        use_language = (non_local_language or language)

        for c in osm_components:
            for k, v in six.iteritems(c):
                if ':' not in k:
                    continue
                splits = k.split(':')
                if len(splits) > 0 and splits[0] == 'name' and '_' in splits[-1] and splits[-1].split('_', 1)[0] == use_language:
                    language_scripts[splits[-1]] += 1
                elif k == 'name' or (splits[0] == 'name' and splits[-1]) == use_language:
                    language_scripts[None] += 1

        language_script = None

        if len(language_scripts) > 1:
            cumulative = float(sum(language_scripts.values()))
            values = list(language_scripts.keys())
            probs = cdf([float(c) / cumulative for c in language_scripts.values()])
            language_script = weighted_choice(values, probs)

        if not language_script and not non_local_language and not more_than_one_official_language:
            return ''
        else:
            return ':{}'.format(language_script or non_local_language or language)

    # e.g. Dublin 3
    dublin_postal_district_regex_str = '(?:[1-9]|1[1-9]|2[0-4]|6w)'
    dublin_postal_district_regex = re.compile('^{}$'.format(dublin_postal_district_regex_str), re.I)
    dublin_city_district_regex = re.compile('dublin {}$'.format(dublin_postal_district_regex_str), re.I)

    @classmethod
    def format_dublin_postal_district(cls, address_components):
        '''
        Dublin postal districts
        -----------------------

        Since the introduction of the Eire code, former Dublin postcodes
        are basically being used as what we would call a city_district in
        libpostal, so fix that here.

        If addr:city is given as "Dublin 3", make it city_district instead
        If addr:city is given as "Dublin" or "City of Dublin" and addr:postcode
        is given as "3", remove city/postcode and make it city_district "Dublin 3"
        '''

        city = address_components.get(AddressFormatter.CITY)
        # Change to city_district
        if city and cls.dublin_city_district_regex.match(city):
            address_components[AddressFormatter.CITY_DISTRICT] = address_components.pop(AddressFormatter.CITY)
            postcode = address_components.get(AddressFormatter.POSTCODE)
            if postcode and (cls.dublin_postal_district_regex.match(postcode) or cls.dublin_city_district_regex.match(postcode)):
                address_components.pop(AddressFormatter.POSTCODE)
            return True
        elif city and city.lower() in ('dublin', 'city of dublin', 'dublin city') and AddressFormatter.POSTCODE in address_components:
            postcode = address_components[AddressFormatter.POSTCODE]
            if cls.dublin_postal_district_regex.match(postcode):
                address_components.pop(AddressFormatter.CITY)
                address_components[AddressFormatter.CITY_DISTRICT] = 'Dublin {}'.format(address_components.pop(AddressFormatter.POSTCODE))
                return True
            elif cls.dublin_city_district_regex.match(postcode):
                address_components[AddressFormatter.CITY_DISTRICT] = address_components.pop(AddressFormatter.POSTCODE)
                return True
        return False

    # e.g. Kingston 5
    kingston_postcode_regex = re.compile('(kingston )?([1-9]|1[1-9]|20|c\.?s\.?o\.?)$', re.I)

    @classmethod
    def format_kingston_postcode(cls, address_components):
        '''
        Kingston postcodes
        ------------------
        Jamaica does not have a postcode system, except in Kingston where
        there are postal zones 1-20 plus the Central Sorting Office (CSO).

        These are not always consistently labeled in OSM, so normalize here.

        If city is given as "Kingston 20", separate into city="Kingston", postcode="20"
        '''
        city = address_components.get(AddressFormatter.CITY)
        postcode = address_components.get(AddressFormatter.POSTCODE)

        if city:
            match = cls.kingston_postcode_regex.match(city)
            if match:
                city, postcode = match.groups()
                if city:
                    address_components[AddressFormatter.CITY] = city
                else:
                    address_components.pop(AddressFormatter.CITY)

                if postcode:
                    address_components[AddressFormatter.POSTCODE] = postcode

                return True
        elif postcode:
            match = cls.kingston_postcode_regex.match(postcode)
            if match:
                city, postcode = match.groups()
                if city and AddressFormatter.CITY not in address_components:
                    address_components[AddressFormatter.CITY] = city

                if postcode:
                    address_components[AddressFormatter.POSTCODE] = postcode

                return True
        return False

    bucharest_city_district_regex = re.compile(u'^(?:Sector(?:ul)? ([1-6]))$', re.I | re.U)

    @classmethod
    def format_bucharest_sector(cls, city_district, language=None):
        if not city_district:
            return None

        city_district = city_district.strip()

        match = cls.bucharest_city_district_regex.match(city_district)
        if not match:
            return city_district

        sector = match.group(1)
        bucharest_sector_config = nested_get(cls.config, ('bucharest_sectors', 'languages', language))
        if not bucharest_sector_config:
            return city_district

        confs, probs = alternative_probabilities(bucharest_sector_config)
        probs_cdf = cdf(probs)

        conf = weighted_choice(confs, probs_cdf)
        ordinal_probability = conf.get('ordinal_probability', 0.0)
        roman_numeral_probability = conf.get('roman_numeral_probability', 0.0)

        if random.random() < ordinal_probability:
            sector = ordinal_expressions.suffixed_number(sector, language)
        elif random.random() < roman_numeral_probability:
            sector = numeric_expressions.roman_numeral(sector)

        direction = conf['direction']
        affix = conf['affix']

        if direction == 'left':
            city_district = u'{} {}'.format(affix, sector)
        else:
            city_district = u'{} {}'.format(sector, affix)

        return city_district

    @classmethod
    def format_romanian_city_district(cls, address_components, language=None):
        city_district = address_components.get(AddressFormatter.CITY_DISTRICT)
        if city_district:
            city_district = cls.format_bucharest_sector(city_district, language=language)
            if city_district:
                address_components[AddressFormatter.CITY_DISTRICT] = city_district
            else:
                address_components.pop(AddressFormatter.CITY_DISTRICT)

            return True
        return False

    french_postal_code_regex = re.compile(u'\\b[0-9]{5}\\b')

    @classmethod
    def extract_french_postcode(cls, address_components):
        postcode = address_components.get(AddressFormatter.POSTCODE)
        if postcode and not is_numeric(postcode):
            match = cls.french_postal_code_regex.search(postcode)
            if match:
                address_components[AddressFormatter.POSTCODE] = match.group(0)

    french_arrondissement_number_regex = re.compile(u'\\b([0-9]+)[a-z]* Arrondissement', re.I)

    @classmethod
    def french_arrondissement_number(cls, neighborhoods):
        for props in neighborhoods:
            if props['component'] == AddressFormatter.CITY_DISTRICT:
                name = props.get('name')
                if name:
                    name = name.strip()
                    match = cls.french_arrondissement_number_regex.search(name)
                    if match:
                        return match.group(1)
        return None

    @classmethod
    def french_arrondissement_roman_numeral(cls, arrondissement, with_ordinal_suffix=True):
        roman_numeral = numeric_expressions.roman_numeral(arrondissement)
        if roman_numeral and not with_ordinal_suffix:
            return roman_numeral
        elif with_ordinal_suffix:
            ordinal_suffix = ordinal_expressions.get_suffix(arrondissement, FRENCH)
            if roman_numeral and ordinal_suffix:
                return u'{}{}'.format(safe_decode(roman_numeral), safe_decode(ordinal_suffix))
        return None

    @classmethod
    def add_cedex_france(cls, address_components, arrondissement=None):
        postcode = address_components.get(AddressFormatter.POSTCODE)
        city = address_components.get(AddressFormatter.CITY)

        if city and postcode:
            cedex_phrase = u'CEDEX' if not arrondissement else u'CEDEX {}'.format(safe_decode(arrondissement).zfill(2))
            address_components[AddressFormatter.CEDEX] = cedex_phrase

    japan_neighborhood_classes = set([AddressFormatter.JAPAN_MAJOR_NEIGHBORHOOD,
                                      AddressFormatter.JAPAN_MINOR_NEIGHBORHOOD,
                                      AddressFormatter.SUBURB])

    japanese_number_regex_str = u'[0-9０-９一二三四五六七八九十百]+'

    japan_major_neighborhood_regex = re.compile(u'(?:.*?(?:町|駅南?))')
    japan_minor_neighborhood_regex = re.compile(u'(?:南{}条西)?{}丁目'.format(japanese_number_regex_str, japanese_number_regex_str))

    japan_city_district_regex = re.compile(u'^.*区$')

    @classmethod
    def extract_japanese_neighborhoods_from_street(cls, address_components):
        street = address_components.get(AddressFormatter.ROAD)
        if street:
            street = safe_decode(street.strip())
            match = cls.japan_minor_neighborhood_regex.search(street)
            if match:
                minor_neighborhood = match.group(0)
                if AddressFormatter.JAPAN_MINOR_NEIGHBORHOOD not in address_components:
                    address_components[AddressFormatter.JAPAN_MINOR_NEIGHBORHOOD] = minor_neighborhood
                street = cls.japan_minor_neighborhood_regex.sub(u'', street)

            match = cls.japan_major_neighborhood_regex.search(street)
            if match:
                major_neighborhood = match.group(0)
                if AddressFormatter.JAPAN_MAJOR_NEIGHBORHOOD not in address_components:
                    address_components[AddressFormatter.JAPAN_MAJOR_NEIGHBORHOOD] = major_neighborhood
                street = cls.japan_major_neighborhood_regex.sub(u'', street)

            if street and street.strip():
                address_components[AddressFormatter.ROAD] = street.strip()
            else:
                address_components.pop(AddressFormatter.ROAD)

    @classmethod
    def format_japanese_neighborhood_romaji(cls, address_components):
        neighborhood = safe_decode(address_components.get(AddressFormatter.JAPAN_MINOR_NEIGHBORHOOD, ''))
        if neighborhood.endswith(safe_decode('丁目')):
            neighborhood = neighborhood[:-2]
            if neighborhood and neighborhood.isdigit():
                if random.random() < 0.5:
                    neighborhood = Digits.rewrite_standard_width(neighborhood)
                suffix = safe_decode(random.choice(('chōme', 'chome')))
                hyphen = six.u('-') if random.random < 0.5 else six.u(' ')
                address_components[AddressFormatter.SUBURB] = six.u('{}{}{}').format(neighborhood, hyphen, suffix)

    japanese_node_admin_level_map = {
        'quarter': 9,
        'neighborhood': 10,
        'neighbourhood': 10,
    }

    @classmethod
    def japanese_neighborhood_sort_key(cls, val):
        admin_level = val.get('admin_level')
        if admin_level and admin_level.isdigit():
            return int(admin_level)
        else:
            return cls.japanese_node_admin_level_map.get(val.get('place'), 1000)

    chinese_numbers = u''.join([rule[0]['name'] for rule in numeric_expressions.cardinal_rules['zh'].values() if rule[0]['value'] < 1000000])

    chinese_house_number_regex = re.compile(u'^.*?([0-9０-９]+[號号])', re.I | re.U)

    chinese_building_regex = re.compile(u'((?:[0-9０-９]+|[a-z])栋)', re.I | re.U)
    taiwan_unit_regex = re.compile(u'(之[0-9０-９{}]+[號号]?)'.format(chinese_numbers), re.UNICODE)
    chinese_unit_regex = re.compile(u'([0-9０-９]+室)', re.U)
    chinese_floor_regex = re.compile(u'((?:[0-9０-９{}]+?[楼樓层層])|(?:地下室))'.format(chinese_numbers), re.UNICODE)

    comma_space_regex = re.compile(u'\s*,?\s*')

    @classmethod
    def regex_match(cls, regex, value):
        if not value or not regex:
            return None
        value = value.strip()
        return regex.search(value)

    @classmethod
    def extract_regex(cls, regex, value, count=None):
        match = cls.regex_match(regex, value)
        if match:
            sep = match.group(0)
            if not count:
                value = regex.sub(u'', value)
            else:
                value = regex.sub(u'', value, count)
            return cls.cleanup_value_post_extraction(value), sep
        return value, None

    @classmethod
    def get_numeric_regex(cls, numeric_class, language, country=None):
        regex = cls.numeric_phrase_regexes.get((numeric_class.key, language, country))
        if not regex:
            regex_str = numeric_class.numeric_regex(language, country=country)
            if not regex_str:
                return None
            regex = re.compile(regex_str, re.I | re.U)
            cls.numeric_phrase_regexes[(numeric_class.key, language, country)] = regex
        return regex

    @classmethod
    def format_chinese_address(cls, address_components):
        new_components = {}
        for field in (AddressFormatter.HOUSE_NUMBER, AddressFormatter.HOUSE, AddressFormatter.BUILDING, AddressFormatter.UNIT):
            original_field = address_components.get(field)
            if original_field:
                current_field = safe_decode(original_field)
                current_field, building = cls.extract_regex(cls.chinese_building_regex, current_field)
                current_field, level = cls.extract_regex(cls.chinese_floor_regex, current_field)
                current_field, unit = cls.extract_regex(cls.chinese_unit_regex, current_field)
                if not unit:
                    current_field, unit = cls.extract_regex(cls.taiwan_unit_regex, current_field)

                current_field = current_field.strip()
                if current_field != original_field.strip():
                    if current_field.strip():
                        address_components[field] = current_field
                    else:
                        address_components.pop(field, None)
                if unit and AddressFormatter.UNIT not in new_components:
                    new_components[AddressFormatter.UNIT] = unit
                if level and AddressFormatter.LEVEL not in new_components:
                    new_components[AddressFormatter.LEVEL] = level
                if building and AddressFormatter.BUILDING not in new_components:
                    new_components[AddressFormatter.BUILDING] = building

        address_components.update(new_components)

        house_number = address_components.get(AddressFormatter.HOUSE_NUMBER)
        if house_number and not cls.is_valid_house_number(house_number):
            match = cls.chinese_house_number_regex.match(house_number)
            if match:
                address_components[AddressFormatter.HOUSE_NUMBER] = match.group(1)
            else:
                address_components.pop(AddressFormatter.HOUSE_NUMBER)

    @classmethod
    def extract_field(cls, value, numeric_class, language, country=None, only_if_last=False):
        regex = cls.get_numeric_regex(numeric_class, language, country=country)
        if not regex:
            return value, None
        prev_match = None
        matches = []

        for match in regex.finditer(value):
            if prev_match and not only_if_last:
                prev_end = prev_match.end()
                start = match.start()
                if start > prev_end + 1:
                    mid = value[prev_end:start]
                    if cls.comma_space_regex.match(mid):
                        matches.append(mid)
                    else:
                        matches.append(u' ')

            matches.append(match.group(0))
            prev_match = match

        if not matches:
            return value, None
        else:
            if only_if_last:
                if not cls.cleanup_value_post_extraction(value[prev_match.end():]):
                    return cls.cleanup_value_post_extraction(value[:prev_match.start()]), matches[-1]
                else:
                    return value, None
            return cls.cleanup_value_post_extraction(regex.sub(u'', value)), u''.join(matches)

    @classmethod
    def extract_numbered_building_from_name(cls, address_components, key, language, country=None):
        name = address_components.get(key)
        if not name:
            return
        name, extracted = cls.extract_field(name, Building, language, country=country, only_if_last=True)
        if extracted:
            if name:
                address_components[key] = name
            else:
                address_components.pop(key)
            if AddressFormatter.BUILDING not in address_components:
                address_components[AddressFormatter.BUILDING] = extracted

    @classmethod
    def name_is_numbered_building(cls, name, languages, country=None):
        for language in languages:
            name, extracted = cls.extract_field(name, Building, language, country=country)
        return not cls.cleanup_value_post_extraction(name)

    @classmethod
    def genitive_name(cls, name, language):
        morph = cls.slavic_morphology_analyzers.get(language)
        if not morph:
            return None
        norm = []
        words = safe_decode(name).split()
        n = len(words)

        for word in words:
            parsed = morph.parse(word)[0]
            inflected = parsed.inflect({'gent'})
            if inflected and inflected.word:
                norm.append(inflected.word)
            else:
                norm.append(word)
        return six.u(' ').join(norm)

    @classmethod
    def add_genitives(cls, address_components, language):
        if language in cls.slavic_morphology_analyzers and AddressFormatter.CITY in address_components:
            for component in address_components:
                if component not in AddressFormatter.BOUNDARY_COMPONENTS:
                    continue
                genitive_probability = nested_get(cls.config, ('slavic_names', component, 'genitive_probability'), default=None)
                if genitive_probability is not None and random.random() < float(genitive_probability):
                    address_components[component] = cls.genitive_name(address_components[component], language)

    @classmethod
    def spanish_street_name(cls, street):
        '''
        Most Spanish street names begin with Calle officially
        but since it's so common, this is often omitted entirely.
        As such, for Spanish-speaking places with numbered streets
        like Mérida in Mexico, it would be legitimate to have a
        simple number like "27" for the street name in a GIS
        data set which omits the Calle. However, we don't really
        want to train on "27/road 1/house_number" as that's not
        typically how a numeric-only street would be written. However,
        we don't want to neglect entire cities like Mérida which are
        predominantly a grid, so add Calle (may be abbreviated later).
        '''
        if is_numeric(street):
            street = six.u('Calle {}').format(street)
        return street

    BRASILIA_RELATION_ID = '2758138'

    @classmethod
    def is_in(cls, osm_components, component_id, component_type='relation'):
        for c in osm_components:
            if c.get('type') == component_type and c.get('id') == component_id:
                return True
        return False

    brasilia_street_name_regex = re.compile('(?:\\s*\-\\s*)?\\b(bloco|bl|lote|lt)\\b.*$', re.I | re.U)
    brasilia_building_regex = re.compile('^\\s*bloco.*$', re.I | re.U)

    @classmethod
    def format_brasilia_address(cls, address_components):
        '''
        Brasília, Brazil's capital, uses a grid-like system
        '''

        street = address_components.get(AddressFormatter.ROAD)
        if street:
            address_components[AddressFormatter.ROAD] = street = cls.brasilia_street_name_regex.sub(six.u(''), street)

        house_number = address_components.get(AddressFormatter.HOUSE_NUMBER)

        if house_number and cls.brasilia_building_regex.match(name):
            address_components[AddressFormatter.BUILDING] = address_components.pop(AddressFormatter.HOUSE_NUMBER)

        name = address_components.get(AddressFormatter.HOUSE)

        if name and cls.brasilia_building_regex.match(name):
            address_components[AddressFormatter.BUILDING] = address_components.pop(AddressFormatter.HOUSE)

    central_european_cities = {
        # Czech Republic
        'cz': [u'praha', u'prague'],
        # Poland
        'pl': [u'kraków', u'crakow', u'krakow'],
        # Hungary
        'hu': [u'budapest'],
        # Slovakia
        'sk': [u'bratislava', u'košice', u'kosice'],
        # Austria
        'at': [u'wien', u'vienna', u'graz', u'linz', u'klagenfurt'],
        # Romania
        'ro': [u'bucurești', u'bucuresti', u'bucharest']
    }
    central_european_city_district_regexes = {country: re.compile(u'^({})\s+(?:[0-9]+|[ivx]+\.?)\\s*$'.format(u'|'.join(cities)), re.I | re.U)
                                              for country, cities in six.iteritems(central_european_cities)}

    @classmethod
    def format_central_european_city_district(cls, country, address_components):
        city = address_components.get(AddressFormatter.CITY)
        city_district_regexes = cls.central_european_city_district_regexes.get(country)
        if city and city_district_regexes:
            match = city_district_regexes.match(city)
            if match:
                address_components[AddressFormatter.CITY_DISTRICT] = address_components.pop(AddressFormatter.CITY)
                address_components[AddressFormatter.CITY] = match.group(1)

    unit_type_regexes = {}
    superblock_regexes = {}
    block_number_regexes = {}
    number_block_regexes = {}
    lot_regexes = {}

    language_number_phrases = {}
    language_block_phrases = {}
    language_lot_phrases = {}

    numeric_pattern = '(?:[\d]+|(?:[a-z]*[\d]+[a-z]*[\-\./])?[\d]+|[\d]+(?:[\-\./][a-z]*[\d]+[a-z]*)|[a-z])'

    lang_phrase_dictionaries = [lang for lang, dictionary_type in six.iterkeys(address_phrase_dictionaries.phrases)]

    phrase_number_pattern = u'\\b(?:{})[\\.\\s]\\s*(?:(?:{})[\\s\\.]?)?\\s*{}\\b'
    number_phrase_pattern = u'\\b(?:(?:{})[\\s\\.]?)?\\s*{}\\s*(?:{})[\\.]?\\b'

    for lang in lang_phrase_dictionaries:
        numbers = address_phrase_dictionaries.phrases.get((lang, 'number'), [])
        numbered_units = address_phrase_dictionaries.phrases.get((lang, 'unit_types_numbered'), [])
        numbered_superblocks = address_phrase_dictionaries.phrases.get((lang, 'superblocks'), [])
        numbered_blocks = address_phrase_dictionaries.phrases.get((lang, 'blocks'), [])
        numbered_lots = address_phrase_dictionaries.phrases.get((lang, 'lots'), [])

        house_numbers = address_phrase_dictionaries.phrases.get((lang, 'house_numbers'), [])

        number_phrases = sorted([safe_decode(p) for p in itertools.chain(*numbers)], reverse=True)
        language_number_phrases[lang] = number_phrases
        house_number_phrases = set([safe_decode(p) for p in itertools.chain(*house_numbers)])
        unit_phrases = sorted([safe_decode(p) for p in itertools.chain(*numbered_units) if len(p) > 2 and p not in house_number_phrases], reverse=True)

        superblock_phrases = sorted([safe_decode(p) for p in itertools.chain(*numbered_superblocks)], reverse=True)
        block_phrases = sorted([safe_decode(p) for p in itertools.chain(*numbered_blocks)], reverse=True)
        lot_phrases = sorted([safe_decode(p) for p in itertools.chain(*numbered_lots)], reverse=True)

        pattern = re.compile(phrase_number_pattern.format(u'|'.join(unit_phrases), u'|'.join(number_phrases), safe_decode(numeric_pattern)),
                             re.I | re.UNICODE)
        unit_type_regexes[lang] = pattern

        if superblock_phrases:
            pattern = re.compile(phrase_number_pattern.format(u'|'.join(superblock_phrases), u'|'.join(number_phrases), safe_decode(numeric_pattern)),
                                 re.I | re.UNICODE)
            superblock_regexes[lang] = pattern

        if block_phrases:
            pattern = re.compile(phrase_number_pattern.format(u'|'.join(block_phrases), u'|'.join(number_phrases), safe_decode(numeric_pattern)),
                                 re.I | re.UNICODE)
            block_number_regexes[lang] = pattern

            pattern = re.compile(number_phrase_pattern.format(u'|'.join(number_phrases), safe_decode(numeric_pattern), u'|'.join(block_phrases)),
                                 re.I | re.UNICODE)
            number_block_regexes[lang] = pattern

        if lot_phrases:
            pattern = re.compile(phrase_number_pattern.format(u'|'.join(lot_phrases), u'|'.join(number_phrases), safe_decode(numeric_pattern)),
                                 re.I | re.UNICODE)
            lot_regexes[lang] = pattern

    english_streets = address_phrase_dictionaries.phrases.get((ENGLISH, 'street_types'), [])
    english_directionals = address_phrase_dictionaries.phrases.get((ENGLISH, 'directionals'), [])
    english_numbered_route_regex = re.compile('\\b(highway|route|county|state|township|farm|ranch)\\b')

    english_numbered_route_canonicals = set([safe_encode(streets[0]) for streets in english_streets if english_numbered_route_regex.search(streets[0])])
    english_numbered_route_phrases = set([safe_encode(p) for p in itertools.chain(*[streets for streets in english_streets if streets[0] in english_numbered_route_canonicals])])
    english_street_phrases = sorted([safe_encode(p) for p in itertools.chain(*(english_streets + english_directionals)) if safe_encode(p) not in english_numbered_route_phrases], reverse=True)
    english_numbered_unit_suffix_regex = re.compile('^(.+ (?:{}))\s*#\s*\\b(?:[\d]+|[a-z]|[a-z]\s*[\d]*\-?[\d]+|[\d]+\-?[\d]*\s*[a-z]|[\d]+\-[\d]+)\\b\s*$'.format(safe_encode('|').join(english_street_phrases)), re.I)
    english_unit_suffix_regex = re.compile('^(.+ (?:{}))\s*\\b(?:[\d]+|[a-z]|[a-z][\d]*\-?[\d]+|[\d]+\-?[\d]*\s*[a-z]|[\d]+\-[\d]+)\\b\s*$'.format(safe_encode('|').join(english_street_phrases)), re.I)

    federal_highway_regex = re.compile('^u\.?s\.?|ca$', re.I)

    @classmethod
    def strip_english_unit_number_suffix(cls, value):
        match = cls.english_numbered_unit_suffix_regex.match(value)
        if match:
            candidate_value = match.group(1)
            norm_tokens = normalized_tokens(candidate_value,
                                            string_options=NORMALIZE_STRING_LOWERCASE,
                                            token_options=TOKEN_OPTIONS_DROP_PERIODS,
                                            strip_parentheticals=False)

            have_non_boilerplate_word = False
            first_phrase = True
            for t, c, length, data in reversed(list(street_boilerplate_gazetteer.filter(norm_tokens))):
                if c == token_types.PHRASE:
                    if first_phrase:
                        first_phrase = False
                        for d in data:
                            _, _, _, canonical = safe_decode(d).split(u'|')
                            if canonical in cls.english_numbered_route_canonicals:
                                # Definitely valid e.g. County Road #12
                                return value
                    continue
                elif c in token_types.WORD_TOKEN_TYPES and not cls.federal_highway_regex.match(t):
                    have_non_boilerplate_word = True

                if have_non_boilerplate_word:
                    break

            # If there's something other than a street phrase, can see "Road #2" in some places
            if have_non_boilerplate_word:
                value = candidate_value

        return value

    @classmethod
    def abbreviated_state(cls, state, country, language):
        abbreviate_state_prob = float(nested_get(cls.config, ('state', 'abbreviated_probability')))

        if random.random() < abbreviate_state_prob:
            state = state_abbreviations.get_abbreviation(country, language, state, default=state)
        return state

    @classmethod
    def abbreviate_admin_components(cls, address_components, country, language, hyphenation=True):
        abbreviate_full_toponym_prob = float(nested_get(cls.config, ('boundaries', 'abbreviate_full_toponym_probability')))
        abbreviate_toponym_prob = float(nested_get(cls.config, ('boundaries', 'abbreviate_toponym_probability')))

        for component, val in six.iteritems(address_components):
            if component not in AddressFormatter.BOUNDARY_COMPONENTS:
                continue

            if component == AddressFormatter.STATE:
                abbreviated = cls.abbreviated_state(val, country, language)
                if abbreviated != val:
                    address_components[component] = abbreviated
                    continue

            abbreviated = abbreviate(toponym_gazetteer, val, language, abbreviate_prob=abbreviate_full_toponym_prob)
            if abbreviated == val:
                abbreviated = abbreviate(toponym_abbreviations_gazetteer, val, language, abbreviate_prob=abbreviate_toponym_prob)

            val = abbreviated

            if hyphenation:
                val = cls.name_hyphens(val)
            address_components[component] = val

    @classmethod
    def valid_place_points(cls, city_point_components, containing_components, country, max_num_cities=2, max_num_villages=3, max_num_suburbs=2):
        city_replacements = place_config.city_replacements(country)

        is_japan = country == Countries.JAPAN

        num_villages = 0
        num_cities = 0
        num_suburbs = 0

        valid = []

        seen_components = set()

        for props in city_point_components:
            component = cls.categorize_osm_component(country, props, containing_components)
            if component is None:
                continue

            is_village = cls.osm_component_is_village(props)

            if ((component == AddressFormatter.CITY or (component in city_replacements and component != AddressFormatter.SUBURB) ) and num_cities < max_num_cities) and not is_village:
                valid.append((component, props))
                seen_components.add(component)
                num_cities += 1

            if is_village and num_villages < max_num_villages:
                valid.append((AddressFormatter.LOCALITY, props))
                seen_components.add(component)
                num_villages += 1

            if is_japan and component in cls.japan_neighborhood_classes and component not in seen_components:
                valid.append((component, props))
                seen_components.add(component)

            if not is_japan and component == AddressFormatter.SUBURB and num_suburbs < max_num_suburbs:
                valid.append((component, props))
                seen_components.add(component)
                num_suburbs += 1

        return valid

    @classmethod
    def add_city_and_equivalent_from_points(cls, grouped_components, city_point_components, containing_components, country):
        city_replacements = place_config.city_replacements(country)

        is_japan = country == Countries.JAPAN

        first_village = None

        for (component, props) in cls.valid_place_points(city_point_components, containing_components, country):
            have_sub_city = any((key in grouped_components and key in city_replacements for key in (AddressFormatter.SUBURB, AddressFormatter.CITY_DISTRICT, AddressFormatter.LOCALITY)))

            have_city = AddressFormatter.CITY in grouped_components

            is_village = cls.osm_component_is_village(props)

            if (component == AddressFormatter.CITY or (component in city_replacements and component != AddressFormatter.SUBURB and not have_city)) and component not in grouped_components and not is_village:
                grouped_components[component].append(props)

            if is_village and not first_village:
                first_village = props

            if not is_japan and component == AddressFormatter.SUBURB:
                grouped_components[component].append(props)

            if is_japan and component in cls.japan_neighborhood_classes:
                existing = grouped_components.get(component, [])
                for p in existing:
                    if (props['id'] == p['id'] and props['type'] == p['type']) or \
                       (props.get('place') in ('neighbourhood', 'neighborhood') and p.get('admin_level') == '10') or \
                       (props.get('place') == 'quarter' and p.get('admin_level') == '9') or \
                       ('place' in p and 'place' in props and props['place'] == p['place']) or \
                       ('name' in props and 'name' in 'p' and props['name'] == p['name']):
                        break
                else:
                    grouped_components[component].append(props)

        if first_village and AddressFormatter.LOCALITY not in grouped_components:
            grouped_components[AddressFormatter.LOCALITY].append(first_village)

    @classmethod
    def add_admin_boundaries(cls, address_components,
                             osm_components,
                             country, language,
                             non_local_language=None,
                             language_suffix='',
                             normalize_languages=None,
                             random_key=True,
                             add_city_points=True,
                             city_point_components=None,
                             drop_duplicate_city_names=True,
                             ):
        '''
        OSM boundaries
        --------------

        For many addresses, the city, district, region, etc. are all implicitly
        generated by the reverse geocoder e.g. we do not need an addr:city tag
        to identify that 40.74, -74.00 is in New York City as well as its parent
        geographies (New York county, New York state, etc.)

        Where possible we augment the addr:* tags with some of the reverse-geocoded
        relations from OSM.

        Since addresses found on the web may have the same properties, we
        include these qualifiers in the training data.
        '''

        suffix_lang = None if not language_suffix else language_suffix.lstrip(':')

        add_prefix_prob = float(nested_get(cls.config, ('boundaries', 'add_prefix_probability')))

        if osm_components:
            name_key = ''.join((boundary_names.DEFAULT_NAME_KEY, language_suffix))
            raw_name_key = boundary_names.DEFAULT_NAME_KEY

            grouped_osm_components = defaultdict(list)

            for i, props in enumerate(osm_components):
                if 'name' not in props:
                    continue

                component = cls.categorize_osm_component(country, props, osm_components)
                if component is None:
                    continue

                admin_center_prob = osm_address_components.use_admin_center.get((props.get('type'), safe_encode(props.get('id', ''))), None)

                if admin_center_prob is not None:
                    if admin_center_prob == 1.0 or random.random() < admin_center_prob:
                        props = props.get('admin_center', props)
                elif 'admin_center' in props:
                    admin_center = {k: v for k, v in six.iteritems(props['admin_center']) if k != 'admin_level'}
                    admin_center_component = cls.categorize_osm_component(country, admin_center, osm_components)
                    if admin_center_component == component and admin_center.get('name') and admin_center['name'].lower() == props.get('name', '').lower():
                        props = props.copy()
                        props.update({k: v for k, v in six.iteritems(admin_center) if k not in props})

                grouped_osm_components[component].append(props)

            poly_components = defaultdict(list)

            existing_city_name = address_components.get(AddressFormatter.CITY)

            if add_city_points and city_point_components and not existing_city_name and AddressFormatter.CITY not in grouped_osm_components:
                cls.add_city_and_equivalent_from_points(grouped_osm_components, city_point_components, osm_components, country)

            city_replacements = place_config.city_replacements(country)
            have_city = AddressFormatter.CITY in grouped_osm_components or set(grouped_osm_components) & set(city_replacements)

            city_names = set([val['name'] for val in grouped_osm_components.get(AddressFormatter.CITY, [])])

            for component, components_values in grouped_osm_components.iteritems():
                seen = set()

                if country == Countries.JAPAN and component == AddressFormatter.SUBURB:
                    components_values = sorted(components_values, key=cls.japanese_neighborhood_sort_key)

                for component_value in components_values:
                    component_name = component_value.get('name', u'')
                    if component == AddressFormatter.CITY and AddressFormatter.LOCALITY in address_components and equivalent(component_name, address_components[AddressFormatter.LOCALITY], toponym_abbreviations_gazetteer, language):
                        continue
                    elif component == AddressFormatter.LOCALITY and component not in address_components and any((equivalent(component_name, city_name, toponym_abbreviations_gazetteer, language) for city_name in city_names)):
                        continue

                    if random_key and not (component in (AddressFormatter.STATE_DISTRICT, AddressFormatter.STATE) and not have_city):
                        key, raw_key = cls.pick_random_name_key(component_value, component, suffix=language_suffix)
                    else:
                        key, raw_key = name_key, raw_name_key

                    for k in (key, name_key, raw_key, raw_name_key):
                        name = component_value.get(k)

                        if name:
                            name_lang = language if not suffix_lang or not k.endswith(language_suffix) else suffix_lang
                            name = boundary_names.name(country, name_lang, component, name, component_value)

                        name_prefix = component_value.get('{}:prefix{}'.format(k, language_suffix))

                        if name and name_prefix and not name.lower().startswith(name_prefix.lower()) and random.random() < add_prefix_prob:
                            name = u' '.join([name_prefix, name])

                        if name and not (name == existing_city_name and component != AddressFormatter.CITY and drop_duplicate_city_names):
                            name = cls.cleaned_name(name, first_comma_delimited_phrase=True)
                            break

                    # if we've checked all keys without finding a valid name, leave this component out
                    else:
                        continue

                    if (component, name) not in seen:
                        poly_components[component].append(name)
                        seen.add((component, name))

            join_state_district_prob = float(nested_get(cls.config, ('state_district', 'join_probability')))
            replace_with_non_local_prob = float(nested_get(cls.config, ('languages', 'replace_non_local_probability')))

            new_admin_components = {}

            is_japan = country == Countries.JAPAN

            for component, vals in poly_components.iteritems():
                if component not in address_components or (non_local_language and random.random() < replace_with_non_local_prob):
                    if random_key:
                        if is_japan and component == AddressFormatter.SUBURB:
                            separator = six.u('') if language != JAPANESE_ROMAJI and non_local_language != ENGLISH else six.u(' ')
                            val = separator.join(vals)
                        elif component == AddressFormatter.STATE_DISTRICT and random.random() < join_state_district_prob:
                            num = random.randrange(1, len(vals) + 1)
                            val = six.u(', ').join(vals[:num])
                        elif len(vals) == 1:
                            val = vals[0]
                        else:
                            val = random.choice(vals)
                    else:
                        val = vals[0]

                    new_admin_components[component] = val

            if normalize_languages is None:
                normalize_languages = []
                if language is not None:
                    normalize_languages.append(language)
            cls.normalize_place_names(new_admin_components, osm_components, country=country, languages=normalize_languages, phrase_from_component=True)

            cls.abbreviate_admin_components(new_admin_components, country, language)

            address_components.update(new_admin_components)

    generic_wiki_name_regex = re.compile('^[a-z]{2,3}:')

    @classmethod
    def unambiguous_wikipedia(cls, osm_component, language):
        name = osm_component.get('name')
        if not name:
            return False

        wiki_name = osm_component.get('wikipedia:{}'.format(language))
        if not wiki_name:
            wiki_name = osm_component.get('wikipedia')
            if wiki_name:
                if (language not in (UNKNOWN_LANGUAGE, AMBIGUOUS_LANGUAGE) and wiki_name.lower().startswith(six.u('{}:'.format(language))) or cls.generic_wiki_name_regex.match(wiki_name)):
                    wiki_name = wiki_name.split(six.u(':'), 1)[-1]

        norm_name = safe_decode(name).strip().lower()

        if not wiki_name and language in (UNKNOWN_LANGUAGE, AMBIGUOUS_LANGUAGE):
            for k, v in six.iteritems(osm_component):
                if k.startswith('wikipedia:') and safe_decode(v).strip().lower() == norm_name:
                    return True
            else:
                return False
        elif not wiki_name:
            return False

        return norm_name == safe_decode(wiki_name).strip().lower()

    def neighborhood_components(self, latitude, longitude):
        return self.neighborhoods_rtree.point_in_poly(latitude, longitude, return_all=True)

    @classmethod
    def add_neighborhoods(cls, address_components, neighborhoods,
                          country, language, non_local_language=None, language_suffix='', replace_city=False):
        '''
        Neighborhoods
        -------------

        In some cities, neighborhoods may be included in a free-text address.

        OSM includes many neighborhoods but only as points, rather than the polygons
        needed to perform reverse-geocoding. We use a hybrid index containing
        Quattroshapes/Zetashapes polygons matched fuzzily with OSM names (which are
        on the whole of better quality).
        '''

        neighborhood_levels = defaultdict(list)

        add_prefix_prob = float(nested_get(cls.config, ('neighborhood', 'add_prefix_probability')))
        use_first_match_prob = float(nested_get(cls.config, ('neighborhood', 'use_first_match_probability')))

        name_key = ''.join((boundary_names.DEFAULT_NAME_KEY, language_suffix))
        raw_name_key = boundary_names.DEFAULT_NAME_KEY

        city_name = address_components.get(AddressFormatter.CITY)

        for neighborhood in neighborhoods:
            component = neighborhood.get('component')

            neighborhood_level = component or AddressFormatter.SUBURB
            if (component not in (AddressFormatter.SUBURB, AddressFormatter.CITY_DISTRICT)):
                continue

            key, raw_key = cls.pick_random_name_key(neighborhood, neighborhood_level, suffix=language_suffix)

            standard_name = neighborhood.get(name_key, neighborhood.get(raw_name_key , six.u('')))
            for k in (key, raw_key, name_key, raw_name_key):
                name = neighborhood.get(k)
                name_prefix = neighborhood.get('{}:prefix'.format(k))
                if name and name_prefix and random.random() < add_prefix_prob:
                    name = u' '.join([name_prefix, name])
                if name:
                    break

            if component == AddressFormatter.CITY_DISTRICT:
                # Optimization so we don't use e.g. same name multiple times for suburb, city_district, city, etc.
                if not replace_city and name == city_name and (not standard_name or standard_name == city_name):
                    continue

            if not name:
                continue

            # For cases like OpenAddresses
            if replace_city and city_name and (equivalent(name, city_name, toponym_abbreviations_gazetteer, language) or equivalent(standard_name, city_name, toponym_abbreviations_gazetteer, language)):
                address_components.pop(AddressFormatter.CITY, None)
                city_name = None
                address_components[neighborhood_level] = name

            neighborhood_levels[neighborhood_level].append(name)

        neighborhood_components = {}

        for component, neighborhoods in neighborhood_levels.iteritems():
            if component not in address_components:
                if len(neighborhoods) == 1 or random.random() < use_first_match_prob:
                    neighborhood_components[component] = neighborhoods[0]
                else:
                    neighborhood_components[component] = random.choice(neighborhoods)

        cls.abbreviate_admin_components(neighborhood_components, country, language)

        address_components.update(neighborhood_components)
        if country == Countries.JAPAN and (language_suffix.endswith(JAPANESE_ROMAJI) or non_local_language == ENGLISH):
            cls.format_japanese_neighborhood_romaji(address_components)

    @classmethod
    def generate_sub_building_component(cls, component, address_components, language, country=None, **kw):
        existing = address_components.get(component, None)

        if existing is None:
            generated_type = cls.generated_type(component, address_components, language, country=country)
            return generated_type

        return None

    @classmethod
    def add_sub_building_phrase(cls, component, phrase_type, address_components, generated, language, country, num_type=None, num_type_props=None, **kw):
        if not generated and not phrase_type != cls.STANDALONE_PHRASE:
            return

        component_class = cls.sub_building_component_class_map[component]

        if generated or phrase_type == cls.STANDALONE_PHRASE:
            if num_type == component_class.DIRECTIONAL:
                if num_type_props:
                    kw.update(
                        direction=num_type_props.get('direction'),
                        direction_probability=num_type_props.get('direction_probability', 0.0)
                    )

            phrase = component_class.phrase(generated, language, country=country, num_type=num_type, **kw)

            if phrase:
                address_components[component] = phrase
        elif component in address_components:
            existing = address_components[component]
            phrase = cls.get_component_phrase(component_class, existing, language, country=country)
            if phrase and phrase != existing:
                address_components[component] = phrase
            elif not phrase:
                address_components.pop(component)

    @classmethod
    def add_sub_building_components(cls, address_components, language, country=None, num_floors=None, num_basements=None, zone=None):
        generated_components = set()

        generated = {
            AddressFormatter.BUILDING: None,
            AddressFormatter.ENTRANCE: None,
            AddressFormatter.STAIRCASE: None,
            AddressFormatter.LEVEL: None,
            AddressFormatter.UNIT: None,
        }

        building_phrase_type = cls.generate_sub_building_component(AddressFormatter.BUILDING, address_components, language, country=country)
        if building_phrase_type == cls.ALPHANUMERIC_PHRASE:
            building = Building.random(language, country=country)
            if building:
                generated[AddressFormatter.BUILDING] = building
                generated_components.add(AddressFormatter.BUILDING)
        elif building_phrase_type == cls.STANDALONE_PHRASE:
            generated_components.add(AddressFormatter.BUILDING)

        entrance_phrase_type = cls.generate_sub_building_component(AddressFormatter.ENTRANCE, address_components, language, country=country)
        entrance_alphanumeric_type = entrance_alphanumeric_props = None
        if entrance_phrase_type == cls.ALPHANUMERIC_PHRASE:
            entrance_alphanumeric_type, entrance_alphanumeric_props = Entrance.choose_alphanumeric_type(language, country=country)
            entrance = Entrance.random(language, country=country, num_type=entrance_alphanumeric_type, num_type_props=entrance_alphanumeric_props)
            if entrance:
                generated[AddressFormatter.ENTRANCE] = entrance
                generated_components.add(AddressFormatter.ENTRANCE)
        elif entrance_phrase_type == cls.STANDALONE_PHRASE:
            generated_components.add(AddressFormatter.ENTRANCE)

        staircase_phrase_type = cls.generate_sub_building_component(AddressFormatter.STAIRCASE, address_components, language, country=country)
        staircase_alphanumeric_type = staircase_alphanumeric_props = None
        if staircase_phrase_type == cls.ALPHANUMERIC_PHRASE:
            staircase_alphanumeric_type, staircase_alphanumeric_props = Staircase.choose_alphanumeric_type(language, country=country)
            staircase = Staircase.random(language, country=country, num_type=staircase_alphanumeric_type, num_type_props=staircase_alphanumeric_props)
            if staircase:
                generated[AddressFormatter.STAIRCASE] = staircase
                generated_components.add(AddressFormatter.STAIRCASE)
        elif staircase_phrase_type == cls.STANDALONE_PHRASE:
            generated_components.add(AddressFormatter.STAIRCASE)

        floor = None

        floor_phrase_type = cls.generate_sub_building_component(AddressFormatter.LEVEL, address_components, language, country=country, num_floors=num_floors, num_basements=num_basements)
        floor_alphanumeric_type = floor_alphanumeric_props = None
        if floor_phrase_type == cls.ALPHANUMERIC_PHRASE:
            floor_alphanumeric_type, floor_alphanumeric_props = Floor.choose_alphanumeric_type(language, country=country)
            floor = Floor.random_int(language, country=country, num_floors=num_floors, num_basements=num_basements)
            floor = Floor.random_from_int(floor, language, country=country,  num_type=floor_alphanumeric_type, num_type_props=floor_alphanumeric_props)
            if floor:
                generated[AddressFormatter.LEVEL] = floor
                generated_components.add(AddressFormatter.LEVEL)
        elif floor_phrase_type == cls.STANDALONE_PHRASE:
            generated_components.add(AddressFormatter.LEVEL)

        unit_phrase_type = cls.generate_sub_building_component(AddressFormatter.UNIT, address_components, language, country=country, num_floors=num_floors, num_basements=num_basements)
        unit_alphanumeric_type = unit_alphanumeric_props = None
        if unit_phrase_type == cls.ALPHANUMERIC_PHRASE:
            unit_alphanumeric_type, unit_alphanumeric_props = Unit.choose_alphanumeric_type(language, country=country)
            unit = Unit.random(language, country=country, num_floors=num_floors, num_basements=num_basements, floor=floor, num_type=unit_alphanumeric_type, num_type_props=unit_alphanumeric_props)

            if unit:
                generated[AddressFormatter.UNIT] = unit
                generated_components.add(AddressFormatter.UNIT)
        elif unit_phrase_type == cls.STANDALONE_PHRASE:
            generated_components.add(AddressFormatter.UNIT)

        # Combine fields like unit/house_number here
        combined = cls.combine_fields(address_components, language, country=country, generated=generated)
        if combined:
            for k in combined:
                generated[k] = None

        cls.add_sub_building_phrase(AddressFormatter.BUILDING, entrance_phrase_type, address_components, generated[AddressFormatter.BUILDING], language, country=country)
        cls.add_sub_building_phrase(AddressFormatter.ENTRANCE, entrance_phrase_type, address_components, generated[AddressFormatter.ENTRANCE], language, country=country, num_type=entrance_alphanumeric_type, num_type_props=entrance_alphanumeric_props)
        cls.add_sub_building_phrase(AddressFormatter.STAIRCASE, staircase_phrase_type, address_components, generated[AddressFormatter.STAIRCASE], language, country=country, num_type=staircase_alphanumeric_type, num_type_props=staircase_alphanumeric_props)
        cls.add_sub_building_phrase(AddressFormatter.LEVEL, floor_phrase_type, address_components, generated[AddressFormatter.LEVEL], language, country=country, num_floors=num_floors, num_type=floor_alphanumeric_type, num_type_props=floor_alphanumeric_props)
        cls.add_sub_building_phrase(AddressFormatter.UNIT, unit_phrase_type, address_components, generated[AddressFormatter.UNIT], language, country=country, zone=zone, num_type=unit_alphanumeric_type, num_type_props=unit_alphanumeric_props)

    @classmethod
    def replace_name_affixes(cls, address_components, language, country=None):
        '''
        Name normalization
        ------------------

        Probabilistically strip standard prefixes/suffixes e.g. "London Borough of"
        '''

        replacement_prob = float(nested_get(cls.config, ('names', 'replace_affix_probability')))

        for component in list(address_components):
            if component not in cls.BOUNDARY_COMPONENTS:
                continue
            name = address_components[component]
            if not name:
                continue

            if random.random() < replacement_prob:
                replacement = name_affixes.replace_affixes(name, language, country=country)
                if replacement != name and not replacement.isdigit():
                    address_components[component] = replacement

    @classmethod
    def replace_names(cls, address_components):
        '''
        Name replacements
        -----------------

        Make a few special replacements (like UK instead of GB)
        '''

        for component, value in address_components.iteritems():
            replacement = nested_get(cls.config, ('value_replacements', component, value), default=None)
            if replacement is not None:
                new_value = repl['replacement']
                prob = repl['probability']
                if random.random() < prob:
                    address_components[component] = new_value

    numeric_boundary_name_regex = re.compile('^[\d]+[a-z]?$', re.I)

    @classmethod
    def remove_numeric_boundary_names(cls, address_components):
        '''
        Numeric boundary name cleanup
        -----------------------------

        Occasionally boundary components may be mislabeled in OSM or another input data set.
        Can look for counterexamples but fairly confident that there are no valid boundary names
        (city, state, etc.) which are all digits. In Japan, neighborhoods are often numbered
        e.g. 1-chome, etc. This can further be combined with a block number and house number
        to form something like 1-3-5. While the combined form is common, the neighborhood would
        not be simply listed as "1" and people expected to understand.
        '''
        for component in list(address_components):
            if component not in cls.BOUNDARY_COMPONENTS or component == AddressFormatter.POSTCODE:
                continue
            value = address_components[component]
            if cls.numeric_boundary_name_regex.match(value.strip()):
                address_components.pop(component)

    @classmethod
    def cleanup_boundary_names(cls, address_components):
        '''
        Boundary name cleanup
        ---------------------

        Cleanup things like addr:city=Rockport,
        '''
        for component in list(address_components):
            if component not in cls.BOUNDARY_COMPONENTS:
                continue

            address_components[component] = address_components[component].strip(six.u(', '))

    @classmethod
    def prune_duplicate_names(cls, address_components):
        '''
        Name deduping
        -------------

        For some cases like "Antwerpen, Antwerpen, Antwerpen"
        that are very unlikely to occur in real life.

        Note: prefer the city name in these cases
        '''

        name_components = defaultdict(list)

        for component in (AddressFormatter.CITY, AddressFormatter.STATE_DISTRICT,
                          AddressFormatter.CITY_DISTRICT, AddressFormatter.SUBURB):
            name = address_components.get(component)
            if name:
                name_components[name.lower()].append(component)

        for name, components in name_components.iteritems():
            if len(components) > 1:
                for component in components[1:]:
                    address_components.pop(component, None)

    @classmethod
    def is_valid_house_number(cls, house_number, country=None):
        if not house_number:
            return False

        house_number = house_number.strip()
        if not house_number:
            return False

        if house_number.isdigit():
            return True

        possible_languages = get_country_languages(country).keys()

        superblock = block = lot = None

        if (not house_number.strip() or house_number.isdigit()) and (superblock or block or lot):
            return True

        norm_tokens = normalized_tokens(house_number, string_options=NORMALIZE_STRING_LOWERCASE,
                                        token_options=TOKEN_OPTIONS_DROP_PERIODS, strip_parentheticals=False)
        last_phrase_was_block = False
        for t, c, length, data in house_number_gazetteer.filter(norm_tokens):
            if c == token_types.NUMERIC:
                prev_class = token_types.WHITESPACE
                for ch in t:
                    if ch.isdigit():
                        current_class = token_types.NUMERIC
                    elif ch.isalpha():
                        current_class = token_types.WORD
                    elif ch == '-':
                        current_class = token_types.HYPHEN
                    elif ch == ',':
                        current_class = token_types.COMMA
                    elif ch == '.':
                        current_class = token_types.PERIOD
                    else:
                        current_class = token_types.OTHER

                    if current_class == token_types.WORD and prev_class == token_types.WORD:
                        return False

                    prev_class = current_class
            elif c == token_types.WORD:
                max_len = 1
                for sub in t.split(u'-'):
                    if len(sub) > max_len:
                        return False
            elif c == token_types.PHRASE:
                valid_phrase = False
                for d in data:
                    lang, dictionary, _, canonical = safe_decode(d).split(u'|')
                    if lang in possible_languages:
                        valid_phrase = True
                        break
                if not valid_phrase:
                    max_len = 1 if not last_phrase_was_block else 2
                    for t_i, c_i in t:
                        for sub in t_i.split(u'-'):
                            if len(sub) > max_len:
                                return False
            elif c not in (token_types.IDEOGRAPHIC_NUMBER, token_types.SLASH, token_types.HYPHEN, token_types.POUND, token_types.COMMA, token_types.PERIOD):
                return False

            if c != token_types.PHRASE:
                last_phrase_was_block = False
        return True

    @classmethod
    def cleaned_name(cls, name, first_comma_delimited_phrase=False):
        '''
        General name cleanup
        --------------------

        Names in OSM and other tagged data sets may contain more than a single
        field. If the field is separated by semicolons, split it and pick one
        of the subfields at random (common in street names). If first_comma_delimited_phrase
        is True, and the phrase has a comma in it, return only the portion of the string
        before the comma.
        '''
        if six.u(';') in name:
            name = random.choice(name.split(six.u(';'))).strip()
        elif first_comma_delimited_phrase and six.u(',') in name:
            name = name.split(six.u(','), 1)[0].strip()
        return name

    stray_dot_regex = re.compile(u'(?<![\w])\.')

    @classmethod
    def cleanup_value_post_extraction(cls, value):
        value = cls.stray_dot_regex.sub(u'', value)
        value = value.strip(u' ,/:').rstrip(six.u('-/'))
        return value

    @classmethod
    def extract_sub_building_components(cls, address_components, key, languages=(), country=None):
        value = address_components.get(key)
        if value is None:
            return

        original_value = value

        for language in languages:

            for (numeric_cls, k) in [(Unit, AddressFormatter.UNIT),
                                     (Floor, AddressFormatter.LEVEL),
                                     (Superblock, AddressFormatter.SUPERBLOCK),
                                     (Building, AddressFormatter.BUILDING),
                                     (POBox, AddressFormatter.PO_BOX),
                                     (Staircase, AddressFormatter.STAIRCASE),
                                     (Entrance, AddressFormatter.ENTRANCE)]:
                value, extracted = cls.extract_field(value, numeric_cls, language, country=country)
                if extracted:
                    if k not in address_components:
                        address_components[k] = extracted
                    else:
                        existing = address_components[k]
                        if random.random() < 0.5:
                            address_components[k] = u'/'.join([existing, extracted])
                        else:
                            address_components[k] = u'/'.join([extracted, existing])

            value = value.strip()

        if value != original_value:
            value = cls.cleanup_value_post_extraction(value)
            if value:
                address_components[key] = value
            else:
                address_components.pop(key)

    numeric_house_number_regex = re.compile(u'^[0-9]+\s*(?:bis|[^\d\W])$', re.I)

    @classmethod
    def cleanup_house_number(cls, address_components, languages=(), country=None):
        '''
        House number cleanup
        --------------------

        This method was originally used for OSM nodes because in some places,
        particularly Uruguay, we see house numbers that are actually a comma-separated
        list. It seemed prudent to retain this cleanup in the generalized version
        in case we see similar issues with other data sets.

        If there's one comma in the house number, allow it as it might
        be legitimate, but if there are 2 or more, just take the first one.
        '''

        if languages:
            languages = list(languages)
            random.shuffle(languages)

        house_number = address_components.get(AddressFormatter.HOUSE_NUMBER)
        if not house_number:
            return

        hn = house_number

        temp_address_components = {AddressFormatter.HOUSE_NUMBER: hn}

        cls.extract_sub_building_components(temp_address_components, AddressFormatter.HOUSE_NUMBER, languages, country=country)
        for k, v in temp_address_components.items():
            if k != AddressFormatter.HOUSE_NUMBER and k not in address_components:
                address_components[k] = v

        if AddressFormatter.HOUSE_NUMBER in temp_address_components:
            address_components[AddressFormatter.HOUSE_NUMBER] = temp_address_components[AddressFormatter.HOUSE_NUMBER]
        else:
            address_components.pop(AddressFormatter.HOUSE_NUMBER, None)

        house_number = address_components.get(AddressFormatter.HOUSE_NUMBER)
        if not house_number:
            return

        house_number = cls.cleanup_value_post_extraction(house_number)

        if not house_number:
            address_components.pop(AddressFormatter.HOUSE_NUMBER, None)
            return

        house_numbers = []
        if u',' in house_number:
            for h in house_number.split(u','):
                h = h.strip()
                if not is_numeric(h) and not cls.numeric_house_number_regex.match(h):
                    house_numbers = []
                    break
                house_numbers.append(h)

        have_semicolon = u';' in house_number
        if have_semicolon:
            house_numbers = [h.strip(u',; ') for h in house_number.split(u';')]

        if house_numbers and (have_semicolon or len(house_numbers) >= 2):
            random.shuffle(house_numbers)
            for num in house_numbers:
                num = num.strip()
                if num:
                    address_components[AddressFormatter.HOUSE_NUMBER] = num
                    break
            else:
                address_components.pop(AddressFormatter.HOUSE_NUMBER, None)

    invalid_street_regex = re.compile('^\s*(?:none|null|not applicable|n\s*/\s*a)\s*$', re.I)

    @classmethod
    def is_valid_street_name(cls, street):
        return street is not None and not (cls.invalid_street_regex.match(street) or not any((c.isalnum() for c in street)))

    @classmethod
    def cleanup_street(cls, address_components, languages=(), country=None):
        street = address_components.get(AddressFormatter.ROAD)
        original_street = street

        cls.extract_sub_building_components(address_components, AddressFormatter.ROAD, languages, country)

        street = address_components.get(AddressFormatter.ROAD)

        if ENGLISH in languages and street:
            street = cls.strip_english_unit_number_suffix(street)

        if street is not None and not cls.is_valid_street_name(street):
            address_components.pop(AddressFormatter.ROAD)

        if street is not None and street != original_street:
            address_components[AddressFormatter.ROAD] = street

    newline_regex = re.compile('[\n]+')
    name_regex = re.compile('^[\s\-]*(.*?)[\s\-]*$')
    whitespace_regex = re.compile('(?<=[\w])[\s]+(?=[\w])')
    hyphen_regex = re.compile('[\s]*[\-]+[\s]*')

    @classmethod
    def dehyphenate_multiword_name(cls, name):
        return cls.hyphen_regex.sub(six.u(' '), name)

    @classmethod
    def hyphenate_multiword_name(cls, name):
        return cls.whitespace_regex.sub(six.u('-'), name)

    @classmethod
    def strip_whitespace_and_hyphens(cls, name):
        name = cls.newline_regex.sub(six.u(' '), name)
        return cls.name_regex.match(name).group(1)

    @classmethod
    def name_hyphens(cls, name, hyphenate_multiword_probability=None, remove_hyphen_probability=None):
        '''
        Hyphenated names
        ----------------

        With some probability, replace hyphens with spaces. With some other probability,
        replace spaces with hyphens.
        '''
        if hyphenate_multiword_probability is None:
            hyphenate_multiword_probability = float(nested_get(cls.config, ('places', 'hyphenate_multiword_probability')))

        if remove_hyphen_probability is None:
            remove_hyphen_probability = float(nested_get(cls.config, ('places', 'remove_hyphen_probability')))

        # Clean string of trailing space/hyphens, the above regex will match any string
        name = cls.strip_whitespace_and_hyphens(name)

        if cls.hyphen_regex.search(name) and random.random() < remove_hyphen_probability:
            return cls.dehyphenate_multiword_name(name)
        elif cls.whitespace_regex.search(name) and random.random() < hyphenate_multiword_probability:
            return cls.hyphenate_multiword_name(name)
        return name

    @classmethod
    def alt_place_names(cls, name, language):
        names = []

        abbrev_name = abbreviate(toponym_abbreviations_gazetteer, name, language, abbreviate_prob=1.0)
        if abbrev_name != name:
            names.append(abbrev_name)

        sans_hyphens = cls.dehyphenate_multiword_name(name)
        if sans_hyphens != name:
            names.append(sans_hyphens)

            abbrev_sans_hyphens = abbreviate(toponym_abbreviations_gazetteer, sans_hyphens, language, abbreviate_prob=1.0)
            if abbrev_sans_hyphens != sans_hyphens:
                names.append(abbrev_sans_hyphens)

                abbrev_hyphens = cls.hyphenate_multiword_name(abbrev_sans_hyphens)
                if abbrev_hyphens != abbrev_sans_hyphens:
                    names.append(abbrev_hyphens)

        with_hyphens = cls.hyphenate_multiword_name(name)
        if with_hyphens != name:
            names.append(with_hyphens)

        if abbrev_name != name:
            abbrev_name_hyphens = cls.hyphenate_multiword_name(abbrev_name)
            if abbrev_name_hyphens != abbrev_name:
                names.append(abbrev_name_hyphens)

        return names

    @classmethod
    def country_specific_cleanup(cls, address_components, country, language=None):
        if country in cls.central_european_city_district_regexes:
            cls.format_central_european_city_district(country, address_components)

        if country == Countries.IRELAND:
            cls.format_dublin_postal_district(address_components)
        elif country == Countries.JAMAICA:
            cls.format_kingston_postcode(address_components)
        elif country == Countries.ROMANIA:
            cls.format_romanian_city_district(address_components, language)

    @classmethod
    def is_simple_number_or_letter(cls, name):
        name = safe_decode(name).strip()
        return is_numeric(name) or (len(name) == 1 and name.isalpha())

    @classmethod
    def add_house_number_phrase(cls, address_components, language, country=None):
        house_number = address_components.get(AddressFormatter.HOUSE_NUMBER, None)
        if not is_numeric(house_number) and (not house_number or house_number.lower() not in cls.latin_alphabet_lower):
            return
        phrase = HouseNumber.phrase(house_number, language, country=country)
        if phrase and phrase != house_number:
            address_components[AddressFormatter.HOUSE_NUMBER] = phrase

    @classmethod
    def add_metro_station_phrase(cls, address_components, language, country=None):
        metro_station = address_components.get(AddressFormatter.METRO_STATION, None)
        phrase = MetroStation.phrase(metro_station, language, country=country)
        if phrase and phrase != metro_station:
            address_components[AddressFormatter.METRO_STATION] = phrase

    @classmethod
    def add_postcode_phrase(cls, address_components, language, country=None):
        postcode = address_components.get(AddressFormatter.POSTCODE, None)
        if postcode:
            phrase = PostCode.phrase(postcode, language, country=country)
            if phrase and phrase != postcode:
                address_components[AddressFormatter.POSTCODE] = phrase

    @classmethod
    def drop_names(cls, address_components):
        return {c: v for c, v in six.iteritems(address_components) if c not in cls.NAME_COMPONENTS}

    @classmethod
    def drop_address(cls, address_components):
        return {c: v for c, v in six.iteritems(address_components) if c not in cls.ADDRESS_LEVEL_COMPONENTS}

    @classmethod
    def drop_places(cls, address_components):
        return {c: v for c, v in six.iteritems(address_components) if c not in place_config.ADMIN_COMPONENTS}

    @classmethod
    def drop_localities(cls, address_components):
        return {c: v for c, v in six.iteritems(address_components) if c not in cls.LOCALITY_COMPONENTS}

    @classmethod
    def drop_postcode(cls, address_components):
        if AddressFormatter.POSTCODE not in address_components:
            return address_components
        return {c: v for c, v in six.iteritems(address_components) if c != AddressFormatter.POSTCODE}

    @classmethod
    def drop_invalid_components(cls, address_components, country):
        if not address_components:
            return
        component_bitset = ComponentDependencies.component_bitset(address_components)

        deps = cls.component_dependencies.get(country, cls.component_dependencies[None])
        dep_order = deps.dependency_order

        for c in dep_order:
            if c not in address_components:
                continue
            if c in deps and not component_bitset & deps[c]:
                address_components.pop(c)
                component_bitset ^= ComponentDependencies.component_bit_values[c]

    @classmethod
    def po_box_address(cls, address_components, language, country=None):
        po_box_config = cls.config['po_box']
        po_box_probability = float(po_box_config['probability'])
        if random.random() < po_box_probability:
            address_components = address_components.copy()
            box_number = POBox.random(language, country=country)
            if box_number is None:
                return None

            po_box = POBox.phrase(box_number, language, country=country)
            if po_box is None:
                return None
            address_components[AddressFormatter.PO_BOX] = po_box
            cls.drop_invalid_components(address_components, country)
            if AddressFormatter.PO_BOX not in address_components:
                return None

            drop_address_probability = po_box_config['drop_address_probability']
            if random.random() < drop_address_probability:
                address_components = cls.drop_address(address_components)

            drop_places_probability = po_box_config['drop_places_probability']
            if random.random() < drop_places_probability:
                address_components = cls.drop_places(address_components)
                address_components = cls.drop_localities(address_components)

            drop_postcode_probability = po_box_config['drop_postcode_probability']
            if random.random() < drop_postcode_probability:
                address_components = cls.drop_postcode(address_components)

            return address_components
        else:
            return None

    @classmethod
    def dropout_places(cls, address_components, osm_components, country, language, population=None, population_from_city=False):
        # Population of the city helps us determine if the city can be used
        # on its own like "Seattle" or "New York" vs. smaller cities like
        # have to be qualified with a state, country, etc.
        unambiguous_city = False

        if population is None and population_from_city:
            population = 0
            tagged = cls.categorized_osm_components(country, osm_components)

            for props, component in (tagged or []):
                if component == AddressFormatter.CITY:
                    if cls.unambiguous_wikipedia(props, language):
                        unambiguous_city = True

                    if 'population' in props:
                        try:
                            population = int(props['population'])
                        except (ValueError, TypeError):
                            continue

        # Perform dropout on places
        address_components = place_config.dropout_components(address_components, osm_components, country=country, population=population, unambiguous_city=unambiguous_city)
        return address_components

    @classmethod
    def dropout_address_level_component(cls, address_components, component):
        probability = cls.address_level_dropout_probabilities.get(component, None)
        if probability is not None and random.random() < probability:
            address_components.pop(component)
            return True
        return False

    @classmethod
    def expanded_with_reverse(cls, address_components, country, candidate_languages,
                              osm_components, neighborhoods, city_point_components,
                              language=None, dropout_places=True, population=None,
                              population_from_city=False, check_city_wikipedia=False,
                              add_sub_building_components=True, hyphenation=True,
                              num_floors=None, num_basements=None, zone=None):
        '''
        Expanded components
        -------------------

        Many times in geocoded address data sets, we get only a few components
        (say street name and house number) plus a lat/lon. There's a lot of information
        in a lat/lon though, so this method "fills in the blanks" as it were.

        Namely, it calls all the methods above to reverse geocode to a few of the
        R-tree + point-in-polygon indices passed in at initialization and adds things
        like admin boundaries, neighborhoods,
        '''

        if country is None:
            return None, None, None

        if not candidate_languages:
            candidate_languages = get_country_lanaguages(country).items()

        if not (country and candidate_languages):
            return None, None, None

        more_than_one_official_language = len(candidate_languages) > 1

        non_local_language = None
        language_suffix = ''

        all_osm_components = osm_components + neighborhoods

        if not language:
            language = cls.address_language(address_components, candidate_languages)
            non_local_language = cls.non_local_language()
            language_suffix = cls.pick_language_suffix(all_osm_components, language, non_local_language, more_than_one_official_language)
        else:
            language_suffix = ':{}'.format(language)

        cls.abbreviate_admin_components(address_components, country, language, hyphenation=hyphenation)

        address_state = cls.state_name(address_components, country, language, non_local_language=non_local_language)
        if address_state:
            address_components[AddressFormatter.STATE] = address_state

        all_languages = set([l for l, d in candidate_languages])


        cls.normalize_place_names(address_components, all_osm_components, country=country, languages=all_languages)

        # If a country was already specified
        cls.replace_country_name(address_components, country, non_local_language or language)

        cls.country_specific_cleanup(address_components, country, language=non_local_language or language)
        if cls.is_in(osm_components, cls.BRASILIA_RELATION_ID):
            cls.format_brasilia_address(address_components)

        if language == CHINESE:
            cls.format_chinese_address(address_components)

        cls.add_admin_boundaries(address_components, osm_components, country, language,
                                 non_local_language=non_local_language,
                                 normalize_languages=all_languages,
                                 language_suffix=language_suffix,
                                 city_point_components=city_point_components)

        cls.add_neighborhoods(address_components, neighborhoods, country, language, non_local_language=non_local_language,
                              language_suffix=language_suffix)

        cls.cleanup_street(address_components, all_languages, country=country)
        street = address_components.get(AddressFormatter.ROAD)

        if language == SPANISH and street:
            norm_street = cls.spanish_street_name(street)
            if norm_street:
                address_components[AddressFormatter.ROAD] = norm_street
                street = norm_street

        cls.cleanup_boundary_names(address_components)

        language_altered = False
        if language_suffix and not non_local_language:
            suffix = language_suffix.lstrip(':').lower()
            if suffix.split('_', 1)[0] in CJK_LANGUAGES:
                language = cls.language_code_aliases.get(suffix, suffix)
                language_altered = True

        cls.replace_name_affixes(address_components, non_local_language or language, country=country)

        cls.replace_names(address_components)

        cls.prune_duplicate_names(address_components)

        all_languages_plus_english = all_languages | set([ENGLISH])

        cls.cleanup_house_number(address_components, all_languages_plus_english, country=country)

        cls.remove_numeric_boundary_names(address_components)

        cls.add_postcode_phrase(address_components, language, country=country)
        cls.add_metro_station_phrase(address_components, language, country=country)

        cls.normalize_sub_building_components(address_components, language, country=country)

        if add_sub_building_components:
            cls.add_sub_building_components(address_components, language, country=country,
                                            num_floors=num_floors, num_basements=num_basements, zone=zone)

        cls.add_house_number_phrase(address_components, language, country=country)

        if dropout_places:
            address_components = cls.dropout_places(address_components, all_osm_components, country, language, population=population, population_from_city=population_from_city)

        cls.drop_invalid_components(address_components, country)

        cls.add_genitives(address_components, language)

        if language_suffix and not non_local_language and not language_altered:
            language = language_suffix.lstrip(':').lower()
            if '_' in language:
                lang, script = language.split('_', 1)
                if lang not in CJK_LANGUAGES and script.lower() not in cls.valid_scripts:
                    language = lang
        elif country in Countries.CJK_COUNTRIES and (non_local_language == ENGLISH or (language_suffix or '').lstrip(':').lower() == ENGLISH):
            language = ENGLISH

        return address_components, country, language

    def expanded(self, address_components, latitude, longitude, language=None,
                 dropout_places=True, population=None,
                 population_from_city=False, check_city_wikipedia=False,
                 add_sub_building_components=True, hyphenation=True,
                 num_floors=None, num_basements=None, zone=None,
                 osm_components=None, country_components=None,
                 neighborhoods=None, city_point_components=None):
        '''
        Expanded components
        -------------------

        Many times in geocoded address data sets, we get only a few components
        (say street name and house number) plus a lat/lon. There's a lot of information
        in a lat/lon though, so this method "fills in the blanks" as it were.

        Namely, it calls all the methods above to reverse geocode to a few of the
        R-tree + point-in-polygon indices passed in at initialization and adds things
        like admin boundaries, neighborhoods,
        '''
        try:
            latitude, longitude = latlon_to_decimal(latitude, longitude)
        except Exception:
            return None, None, None

        if osm_components is None:
            osm_components = self.osm_reverse_geocoded_components(latitude, longitude)

        if country_components is None:
            country_components = self.country_reverse_geocoded_components(latitude, longitude)

        country, candidate_languages = self.osm_country_and_languages(country_components)

        if neighborhoods is None:
            neighborhoods = self.neighborhood_components(latitude, longitude)

        if city_point_components is None:
            city_point_components = self.places_index.nearest_points(latitude, longitude)

        return self.expanded_with_reverse(address_components,
                                          osm_components, neighborhoods, city_point_components,
                                          language=language, dropout_places=dropout_places, population=population,
                                          population_from_city=population_from_city, check_city_wikipedia=check_city_wikipedia,
                                          add_sub_building_components=add_sub_building_components, hyphenation=hyphen,
                                          num_floors=num_floors, num_basements=num_basements, zone=zone,
                                          country=country)

    @classmethod
    def limited_with_reverse(cls, address_components, country, candidate_languages, osm_components, neighborhoods, language=None):
        if country is None:
            return None, None, None

        if not candidate_languages:
            candidate_languages = get_country_lanaguages(country).items()

        if not (country and candidate_languages):
            return None, None, None

        remove_keys = NAME_KEYS + HOUSE_NUMBER_KEYS + POSTAL_KEYS + OSM_IGNORE_KEYS

        for key in remove_keys:
            _ = address_components.pop(key, None)

        more_than_one_official_language = len(candidate_languages) > 1

        if not language:
            language = cls.address_language(value, candidate_languages)

        address_components = cls.normalize_address_components(address_components)

        non_local_language = cls.non_local_language()
        cls.replace_country_name(address_components, country, non_local_language or language)

        address_state = cls.state_name(address_components, country, language, non_local_language=non_local_language, always_use_full_names=True)
        if address_state:
            address_components[AddressFormatter.STATE] = address_state

        all_languages = set([l for l, d in candidate_languages])

        all_osm_components = osm_components + neighborhoods
        language_suffix = cls.pick_language_suffix(all_osm_components, language, non_local_language, more_than_one_official_language)

        cls.normalize_place_names(address_components, all_osm_components, country=country, languages=all_languages)

        cls.add_admin_boundaries(address_components, osm_components, country, language,
                                 language_suffix=language_suffix,
                                 non_local_language=non_local_language,
                                 normalize_languages=all_languages,
                                 random_key=False)

        cls.add_neighborhoods(address_components, neighborhoods, country, language,
                              language_suffix=language_suffix)

        cls.replace_name_affixes(address_components, non_local_language or language, country=country)

        cls.replace_names(address_components)

        cls.prune_duplicate_names(address_components)

        if language_suffix and not non_local_language:
            language = language_suffix.lstrip(':').lower()
            if '_' in language:
                lang, script = language.split('_', 1)
                if lang not in CJK_LANGUAGES and script.lower() not in cls.valid_scripts:
                    language = lang

        return address_components, country, language

    def limited(self, address_components, latitude, longitude, language=None):
        try:
            latitude, longitude = latlon_to_decimal(latitude, longitude)
        except Exception:
            return None, None, None

        osm_components = self.osm_reverse_geocoded_components(latitude, longitude)
        country_components = self.country_reverse_geocoded_components(latitude, longitude)
        country, candidate_languages = self.osm_country_and_languages(country_components)

        neighborhoods = self.neighborhood_components(latitude, longitude)

        return self.limited_with_reverse(address_components, country, candidate_languages, osm_components, neighborhoods, language=language)
