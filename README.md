## Download your bank transactions

### Description

This tool downloads your transactions from banks and stores them into an SQLite database. All processing is happening on your host. No information is transmitted anywhere. Your password is not needed. The tool accesses the bank websites by using your browser's cookies.

It supports checking and savings accounts from the following banks:

* Ally
* Bank Of America
* Chase
* Fidelity
* First Tech Credit Union
* Marcus
* Wells Fargo

Also, it supports credit cards from the following banks:

* American Express
* Bank Of America
* Fidelity

### Usage

Before you execute the script, you have to:

* Edit the `FIREFOX_PROFILE_PATH` variable to point to your Mozilla Firefox profile path.
* Fill the `banks` list in the `main` method with your bank accounts.
* Login to the bank websites with your browser and keep the browser open.
