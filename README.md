# Project Goals
The auditor takes as input a .bib and .tex files, and applies a series of checks on them. The checks are intended to be used:
1. Manually by the paper authors to ensure the absolute best quality of their references
2. Automatically to screen submissions for hallucinated references
3. Automatically by AI Agents who are writing papers to ensure the references are correct and up-to-date

# Audit steps
## 1. Which exact artifact does the refence point to?
1. For papers we want to find DOI - but also keep a list of reputable venues which don't assign it, e. g. TMLR
3. For books, we want to find ISBN
4. For books, we prefer chapter citation, but don't insist
5. For artifacts, we want a URL

The general plan is:
1. Query the databases
2. Apply formal code-based filters (where we are able to derive them)
3. Unless there is returned record is a 100% match, use an LLM to filter the results one-by-one - "can the returned record be correspond to the entry in .bib"?
4. If there are multiple plausable records, use formal code-based filters (where we are able to derive them) to check whether they correspond to the same object, again use LLM as the final step
5. The end result is a very robust and reliable output, one of three:
5.1. The .bib entry doesn't match a real document
5.2. The .bib entry matches exactly one real document
5.3. The .bib entry matches multiple real documents

## 2. Is there a better version of the same artifact?
1. For papers, priority is published > preprint
2. For books, prefer later editions

## 3. Produce the absolute best possible reference
1. Compile all available information
2. Ensure that .bib entries use the most correct and canonical format

# General Design Principles
1. Databases might be incomplete and metadata can sometimes be incorrect (e. g. month). DOI & ISBN uniquely identify documents; title & author list might be spelled slightly differently but are usually correct
2. Modus oprandi: the system will process cases. It will make mistakes (and be corrected). The goal is to use individual failures to improve the systems's overall realiability. Document the quirks of the DBs and data you encounter
3. When fixing a mistake, add it as a unit test. Unit tests should used mocked DB responses
4. Maintain a DB of checks so repeated runs on the same .bib file won't trigger unnecessary LLM & DB calls

# Software Design
1. The overall system is based on https://github.com/constructorfabric/studio
2. The part which queries the DBs should be modular (easy to change and add adapters for individual DBs)
3. .env contains a bunch of API keys
4. The LLM model should be configurable, by defaul use `gpt-5.4-mini`
5. Use `uv` to manage Python dependencies
6. Python code should be a module in `src/`, without relative imports and `sys.path.append`
