# pydabs-genie-agent-retarget

This project allows to rewrite Unity Catalog references in [Databricks Genie
Agent](https://docs.databricks.com/aws/en/genie-agents/) (formerly Genie space)
definitions when you deploy them with Declarative Automation Bundles (DABs).

You can read more about this project in a blog post [Deploying Databricks Genie Agents to multiple environments with DABs](https://alexott.blogspot.com/2026/10/deploying-databricks-genie-agents-to.html).

> [!NOTE]
> We use "agent" and "space" interchangeably in this document and in the source code.

A serialized Genie space hardcodes `catalog.schema.table` names in table identifiers, join
specs, SQL snippets, example and benchmark SQL, and text instructions.
`genie_agent_retarget` is a Python mutator for DABs that rewrites all of them per target
with prefix rules:

| Rule level      | Example                                              |
|-----------------|------------------------------------------------------|
| table or volume | `main.intel.oss_iocs: ${var.catalog}.intel.iocs`     |
| schema          | `main.gold: ${var.catalog}.${var.gold_schema}`       |
| catalog         | `main: ${var.catalog}`                               |

The most specific rule wins, and each name is rewritten once (rules never chain). Matching
is case-insensitive and handles backtick-quoted names, in the space and in the rules. A
rule whose two sides have a different number of parts fails `databricks bundle validate`.

The same rules apply to Unity Catalog volume paths: in volume data sources
(`data_sources.volumes[].path`, used by [Genie Agents that read files in
volumes](https://docs.databricks.com/aws/en/genie-agents/volumes)) and in any text or SQL,
`/Volumes/<catalog>/<schema>/<volume>/...` is matched as the name `catalog.schema.volume`.
With `main.gold: dev.gold_v2`, `/Volumes/main/gold/raw/` becomes `/Volumes/dev/gold_v2/raw/`.
A 3-part rule renames a table or a volume with that name.

After a rewrite, the mutator sorts `data_sources.tables` by identifier and
`data_sources.volumes` by path, because the Genie API rejects an unsorted list. If a table rule renames a table, the mutator also renames
the short table qualifiers in SQL fields: join specs, SQL snippets (filters, measures,
expressions), example SQL, and benchmark SQL (`oss_iocs.ioc_type = 'URL'` becomes
`iocs.ioc_type = 'URL'`). It skips a short name that another table of the space also has,
because it is ambiguous.

## Usage

1. Add the package to the bundle's virtual environment (with `uv`):

   ```toml
   [dependency-groups]
   dev = [
       "databricks-bundles==1.19.0",
       "pydabs-genie-agent-retarget",
   ]
   ```

   `databricks-bundles` is a dependency of `pydabs-genie-agent-retarget`; pin it
   in your project to match your Databricks CLI version. Or copy
   `src/genie_agent_retarget.py` next to `databricks.yml`. It has no
   dependencies except `databricks-bundles`.

2. Register the mutator and define the `genie_table_mapping` variable in
   `databricks.yml`:

   ```yaml
   python:
     venv_path: .venv
     mutators:
       - 'genie_agent_retarget:retarget_genie_spaces'

   variables:
     genie_table_mapping:
       type: complex
       default:
         main.gold: ${var.catalog}.${var.gold_schema}
   ```

3. Keep each space as `src/<name>.geniespace.json` (the output of
   `databricks bundle generate genie-space`) and reference it with `file_path`.

Use `genie_table_mapping: {}` in a target that needs no rewriting.

## Command line

The same code works on a single file. This is useful when you export a space from an
environment that is not the authoring one, and must convert it back to the authoring
names:

```sh
python -m genie_agent_retarget src/x.geniespace.json --in-place \
  -m dev_catalog.gold_dev=main.gold
```

## Example

`example/` deploys 3 Genie spaces (CVE analysis, IoC analysis, and Lakewatch security
operations over 22 OCSF gold tables). Each data source location is a variable
(`ocsf_schema`, `cve_schema`, `iocs_table`):

- `dev` moves the spaces to other schemas and renames one table.
- `azure` deploys them to another workspace, where the data is in other
  catalogs and schemas.
- `prod` keeps the names, so the files are deployed without changes.

```sh
cd example
uv sync
databricks bundle validate -t dev
databricks bundle deploy -t dev
```

The example uses names from our environments: the CLI profiles (`lw-field`, `az-alexott`),
the catalogs and schemas of each target, and the warehouse name.  The JSON files keep the
names they were authored against (`fe_lakewatch_catalog...`), because the rules map from
these names. To try the example in your workspace, change `workspace.profile`,
`warehouse_name`, and the `ocsf_schema`, `cve_schema`, and `iocs_table` variables of a
target to your tables. The tables must have the same columns, otherwise the instructions
and example SQL of the spaces do not match the data.

The tables must exist before you deploy (see [Limitations](#limitations)).

If the `DATABRICKS_CONFIG_PROFILE` environment variable is set, the CLI uses that profile
instead of the target's `workspace.profile`. Unset it when you deploy targets that use
different workspaces.

## Limitations

- The rules work only with 3-part names. If the SQL in a space uses unqualified table
  names (`FROM authentication`), the mutator does not change it, and the query uses the
  default catalog and schema of the session. Genie writes fully-qualified names into
  serialized content by default, so this is mainly a problem for SQL that was written by
  hand.
- A catalog rule rewrites any 3-part name that starts with this catalog, in any field. If
  your catalog has the same name as a struct column (for example, a catalog `actor`), a
  struct path such as `actor.user.name` is also rewritten. In this case, use schema rules
  instead.
- The Genie API checks that all tables of the space exist when it creates or updates the
  space. If a table does not exist, the deployment fails with an error such as `Table
  'main.gold.authentication' does not exist`. So in a new environment, the tables must
  exist before you deploy the space. If the same bundle has the jobs that create these
  tables, keep the spaces in a separate bundle, and deploy it after the jobs ran for the
  first time.
- The mutator changes only table references. Set other values of the space, such as the
  warehouse ID, with fields of the resource declaration and bundle variables.
- Tables, volumes, schemas, and catalogs are retargeted. Other data sources (for example,
  MCP connections) are not changed.
- `databricks bundle generate genie-space --watch` downloads the deployed definition. If
  you edit the space in a target that uses other table names, the downloaded file
  contains those names. Edit the space in the target whose names match the JSON files, or
  convert the file back with the [command line](#command-line).

## Tests

```sh
uv sync && uv run pytest
```

## License

This project is licensed under the [Databricks License](LICENSE).
