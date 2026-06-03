{
    'name': 'Scan Documents Into Odoo',
    'version': '18.0.1.0.0',
    'summary': 'Scan documents directly into Odoo using any locally connected scanner — no plugins, no cloud.',
    'description': """
Direct Document Scanner
=======================
Scan physical documents directly into any Odoo record using your locally
connected scanner hardware (USB, network, ADF).

Key Features
------------
- Works with HP, Canon, Epson, Brother, Xerox, Fujitsu and more
- Supports TWAIN (Windows), WIA (Windows), SANE (Linux), eSCL / ADF (macOS)
- Auto-crop, manual crop, multi-page ADF scanning
- Build multi-page PDFs from scanned pages, all inside Odoo
- 100 %% local — zero cloud dependency, zero data leaves your network
- Native OWL 2 widget — integrates seamlessly with Odoo 18 backend views
- Companion Python bridge: cheque_scanner_bridge.py (included)

Supported Platforms
-------------------
Windows 10 / 11, Ubuntu 20.04+, macOS 12+

Requirements
------------
- Python 3.8+ with websockets and Pillow
- Platform driver: twain + pywin32 (Windows), python-sane (Linux),
  sane-backends via Homebrew (macOS)
    """,

    'category': 'Technical/Document Management',
    'author': 'Alzarough',
    'maintainer': 'Alzarough',
    'website': 'https://youtu.be/eVYmdfLoyVs',
    'support': 'alzarough@gmail.com',

    'license': 'OPL-1',
    'price': 5.0,
    'currency': 'USD',

    'images': [
        'static/description/banner.png',
        'static/description/banner.jpg',
        'static/description/icon.png',
        'static/description/screenshot_01.png',
        'static/description/screenshot_02.png',
        'static/description/screenshot_03.png',
        'static/description/screenshot_04.png',
        'static/description/screenshot_05.png',
        'static/description/screenshot_06.png',
    ],

    'depends': ['mail'],

    'data': [],

    'assets': {
        'web.assets_backend': [
            'scanner_integration/static/src/xml/scanner_image_widget.xml',
            'scanner_integration/static/src/js/scanner_image_widget.js',
        ],
    },
    'external_dependencies': {
        'python': ['websockets', 'PIL'],
    },

    'installable': True,
    'application': True,
    'auto_install': False,
}
