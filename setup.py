#!/usr/bin/env python
# -*- coding: utf-8 -*-
from rdfextras import __version__

config = dict(
    name = 'rdfextras',
    version = __version__,
    description = "RDFExtras provide tools, extra stores and such for RDFLib.",
    author = "Niklas Lindström",
    author_email = "lindstream@gmail.com",
    url = "http://code.google.com/p/rdfextras/",
    license = "BSD",
    platforms = ["any"],
    classifiers = ["Programming Language :: Python",
                   "License :: OSI Approved :: BSD License",
                   "Topic :: Software Development :: Libraries :: Python Modules",
                   "Operating System :: OS Independent",
                   ],
    packages = ['rdfextras',
                'rdfextras.parsers',
                'rdfextras.serializers',
                'rdfextras.tools',
                'rdfextras.tools.Client',
                'rdfextras.sparql',
                'rdfextras.sparql.results',
                'rdfextras.store',
                'rdfextras.store.FOPLRelationalModel',
                'rdfextras.web',],
    package_dir = { 'rdfextras.web': 'rdfextras/web' },
    package_data={ 'rdfextras.web': [
            'templates/*.html',
            'static/*',
]}

)

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup
else:
    config.update(
        entry_points = {
            'console_scripts': [
                'rdfpipe = rdfextras.tools.rdfpipe:main',
            ],
            'nose.plugins': [
                'EARLPlugin = rdfextras.tools.EARLPlugin:EARLPlugin',
            ],
            'rdf.plugins.parser': [
                'rdf-json = rdfextras.parsers.rdfjson:RdfJsonParser',
                'json-ld = rdfextras.parsers.jsonld:JsonLDParser',
            ],
            'rdf.plugins.serializer': [
                'rdf-json = rdfextras.serializers.rdfjson:RdfJsonSerializer',
                'json-ld = rdfextras.serializers.jsonld:JsonLDSerializer', 
            ],
        },
)

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup
else:
    config.update(
        extras_require = {
            "sparql" : ['rdflib >= 3.0', 'pyparsing']
            },
        #test_suite = 'nose.collector',
        #namespace_packages = ['rdfextras'], # TODO: really needed?
        install_requires = [
            'rdflib >= 3.2.0-dev',
            'pyparsing'
        ],
    )

setup(**config)

