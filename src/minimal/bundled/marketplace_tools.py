"""Model tools for the extension marketplace: ext_search, ext_install and ext_publish.

Shipped inside minimal and loaded like any extension, before the others. Turn it off with
`"extensions": {"disabled": ["marketplace_tools"]}` in a settings file: the model then has no way to search,
install or publish extensions, and the prompt stops mentioning them. The user's own `/ext` is not affected.

As first-party code it reaches the marketplace internals directly instead of `minimal.api`.
"""

__version__ = "1.0"
__api__ = "2"

from minimal.extensions import loader, marketplace

CATEGORY_SCHEMA = {
    "type": "string",
    "enum": list(loader.CATEGORIES),
    "description": "extensions (general), themes (colour themes), tools (tools for you) or widgets (UI components)",
}

MARKETPLACE_SCHEMA = {
    "type": "string",
    "description": "Marketplace name or URL, as listed by ext_search",
}


def register(minimal):
    minimal.register_tool(
        name="ext_search",
        run=marketplace.ext_search_tool,
        description=(
            "Search every configured minimal extension marketplace. Returns one line per extension: "
            "category/name, version and description, prefixed by the marketplace when there are several. "
            "Check it before writing a new extension; one may already exist."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words to match in the name or description; empty lists everything",
                },
                "category": CATEGORY_SCHEMA,
            },
        },
    )
    minimal.register_tool(
        name="ext_install",
        run=marketplace.ext_install_tool,
        description=(
            "Install an extension from a marketplace into the global (default) or project extensions folder, "
            "in the subfolder of its category: the newest version written for this minimal's API. It is not "
            "active until you call reload_extensions. An installed copy with local changes is not overwritten "
            "unless force is true."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Extension name, as listed by ext_search",
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "global (every project, default) or project (this folder only)",
                },
                "category": {
                    **CATEGORY_SCHEMA,
                    "description": "Needed only if the name is in several categories",
                },
                "marketplace": {
                    **MARKETPLACE_SCHEMA,
                    "description": "Needed only if several marketplaces have the name",
                },
                "force": {
                    "type": "boolean",
                    "description": "Overwrite an installed copy that has local changes (default false)",
                },
            },
            "required": ["name"],
        },
        confirm=True,
    )
    minimal.register_tool(
        name="ext_publish",
        run=marketplace.ext_publish_tool,
        description=(
            "Publish an extension file or package folder to a marketplace: pushes a new branch and opens a pull "
            "request. It must define register(minimal), start with a docstring (its first line is the description) "
            "and set __version__. It goes in the category of its folder (for example "
            "~/.minimal/extensions/themes) unless one is given; in the marketplace it is filed under <category>/."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The extension file or package folder",
                },
                "name": {
                    "type": "string",
                    "description": "Name to publish under (default: the file name)",
                },
                "category": CATEGORY_SCHEMA,
                "marketplace": {
                    **MARKETPLACE_SCHEMA,
                    "description": "Where to publish (default: $MINIMAL_MARKETPLACE or the default)",
                },
            },
            "required": ["path"],
        },
        confirm=True,
    )
    minimal.append_system_prompt("Share and reuse extensions with ext_search, ext_install and ext_publish.")
