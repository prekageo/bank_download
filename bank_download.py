#!/usr/bin/env python3

import csv
import datetime
import decimal
import hashlib
import html
import io
import json
import logging
import lxml.html
import lz4.block
import os
import re
import sqlite3
import time
import urllib.request
from collections import namedtuple

FIREFOX_PROFILE_PATH = ''

Account = namedtuple('Account', ['id', 'name'])

def parse_amount(amount):
    return decimal.Decimal(amount.replace(',', '').replace('$', ''))

def create_hash(*data):
    to_hash = '\0'.join(repr(d) for d in data)
    ret = hashlib.md5(to_hash.encode('ascii')).hexdigest()
    logging.debug('create_hash %r = %s', data, ret)
    return ret

class WebBrowser:
    def __init__(self, referer, cookie_hosts, headers=None):
        self.referer = referer
        self.cookies = {}
        self.cookies.update(**dict(self.get_firefox_cookies(cookie_hosts)))
        self.cookies.update(**dict(self.get_firefox_cookies_session(cookie_hosts)))
        self.headers = headers

    def get_firefox_cookies(self, cookie_hosts):
        conn = sqlite3.connect(f'{FIREFOX_PROFILE_PATH}/cookies.sqlite')
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute('select * from moz_cookies')
        cookies = []
        for row in cur.fetchall():
            if row['host'] in cookie_hosts or '.' + row['host'] in cookie_hosts:
                yield row['name'], row['value']
        conn.close()
        return cookies

    def get_firefox_cookies_session(self, cookie_hosts):
        filename = f'{FIREFOX_PROFILE_PATH}/sessionstore-backups/recovery.jsonlz4'
        data = open(filename, 'rb').read()
        data = lz4.block.decompress(data[8:])
        data = json.loads(data)
        cookies = []
        for k in data['cookies']:
            if k['host'] in cookie_hosts:
                yield k['name'], k['value']
        return cookies

    def get(self, url, data=None):
        logging.debug('%s %s', 'POST' if data else 'GET', url)
        time.sleep(1)
        r = urllib.request.Request(url)
        r.add_header('Accept', '*/*')
        r.add_header('Accept-Language', 'en-US,en;q=0.5')
        r.add_header('Connection', 'keep-alive')
        r.add_header('Referer', self.referer)
        r.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 6.1; Win64; x64; rv:63.0) Gecko/20100101 Firefox/63.0')
        cookies = []
        for k, v in self.cookies.items():
            cookies.append(f'{k}={v}')
        cookies = '; '.join(cookies)
        r.add_header('Cookie', cookies)
        if self.headers is not None:
            for k, v in self.headers.items():
                r.add_header(k, v)
        return urllib.request.urlopen(r, data)

    def update_cookies(self, response):
        for cookie in response.headers.get_all('Set-Cookie'):
            k, _, v = cookie.partition(';')[0].partition('=')
            self.cookies[k] = v

class Transaction:
    @staticmethod
    def load(conn, account_name, bank_txn_id):
        cur = conn.cursor()
        row = cur.execute('select * from transactions where account_name=? and bank_txn_id=?', (account_name, bank_txn_id)).fetchone()
        if row is None:
            return None
        ret = Transaction()
        for k in row.keys():
            ret.__setattr__(k, row[k])
        ret.date = datetime.datetime.strptime(ret.date, '%Y-%m-%d').date()
        ret.amount = decimal.Decimal(ret.amount)
        return ret

    def save(self, conn):
        cur = conn.cursor()
        cur.execute('insert into transactions (account_name, bank_txn_id, date, category, amount, description) values (?,?,?,?,?,?)', (self.account_name, self.bank_txn_id, self.date.strftime('%Y-%m-%d'), self.category, str(self.amount), self.description))
        conn.commit()

    @staticmethod
    def create_table(conn):
        conn.execute('create table if not exists transactions (id integer primary key autoincrement, account_name, bank_txn_id, date date, category, amount text, description, unique (account_name, bank_txn_id))')

    def matches(self, other):
        assert self.account_name == other.account_name
        assert self.bank_txn_id == other.bank_txn_id
        assert self.amount == other.amount, (self.amount, other.amount)
        if hasattr(other, 'date'):
            assert self.date == other.date
        if hasattr(other, 'description'):
            assert self.description == other.description
        if hasattr(other, 'category'):
            assert self.category == other.category
        return True

class ParsedTransaction:
    def __init__(self, new, txn):
        self.new = new
        self.txn = txn

class Bank:
    def get_transactions(self):
        found_existing_txn = False
        for from_date, to_date in self.walk_time(self.walk_time_fmt):
            logging.debug('walk_time %s %s', from_date, to_date)
            first_page_empty = True
            # TODO: how do you handle idle periods in the account? if you don't find a transaction between june-aug, it doesn't mean there are no txns in may.
            for data in self.walk_pages(from_date, to_date):
                logging.debug('page len=%d', len(data))
                for parsed_txn in self.process_page(data):
                    first_page_empty = False
                    if not parsed_txn.new:
                        found_existing_txn = True
                    yield parsed_txn
            if first_page_empty or found_existing_txn:
                logging.debug('done %d %d', first_page_empty, found_existing_txn)
                break

    def walk_time(self, fmt):
        to_date = datetime.datetime.now().date()
        while True:
            from_date = to_date - datetime.timedelta(days=60)
            yield (from_date.strftime(fmt), to_date.strftime(fmt))
            to_date = from_date

class BankOfAmerica(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        self.browser = WebBrowser('https://secure.bankofamerica.com/', ['.bankofamerica.com', 'secure.bankofamerica.com'])
        self.base_url = 'https://secure.bankofamerica.com'
        self.url = f'{self.base_url}/myaccounts/brain/redirect.go?source=overview&target=acctDetails&adx={self.account_id}'
        self.html_data = self.browser.get(self.url).read()
        # open('tmp.html', 'wb').write(self.html_data)
        # self.html_data = open('tmp.html', 'rb').read()
        self.category_map = {100: 'Business Expenses: Business Miscellaneous', 101: 'Business Expenses: Dues & Subscriptions', 102: 'Business Expenses: Office Maintenance', 103: 'Business Expenses: Office Supplies', 104: 'Business Expenses: Postage & Shipping', 105: 'Business Expenses: Printing', 106: 'Education: Education', 107: 'Finance: Credit Card Payments', 108: 'Finance: Loans', 109: 'Finance: Service Charges/Fees', 110: 'Finance: Taxes', 111: 'Giving: Giving', 112: 'Groceries: Groceries', 113: 'Health: Healthcare/Medical', 114: 'Health: Insurance', 115: 'Home & Utilities: Cable/Satellite Services', 116: 'Home & Utilities: Home Improvement', 117: 'Home & Utilities: Home Maintenance', 118: 'Home & Utilities: Mortgages', 119: 'Home & Utilities: Rent', 120: 'Home & Utilities: Telephone Services', 121: 'Home & Utilities: Utilities', 122: 'Cash, Checks & Misc: ATM/Cash Withdrawals', 123: 'Cash, Checks & Misc: Checks', 124: 'Cash, Checks & Misc: Other Bills', 125: 'Cash, Checks & Misc: Other Expenses', 126: 'Personal & Family Care: Child/Dependent Expenses', 127: 'Personal & Family Care: Personal Care', 128: 'Personal & Family Care: Pets/Pet Care', 129: 'Restaurants & Dining: Restaurants/Dining', 130: 'Savings & Transfers: Savings', 131: 'Savings & Transfers: Securities Trades', 132: 'Savings & Transfers: Transfers', 133: 'Shopping & Entertainment: Clothing/Shoes', 134: 'Shopping & Entertainment: Electronics', 135: 'Shopping & Entertainment: Entertainment', 136: 'Shopping & Entertainment: General Merchandise', 137: 'Shopping & Entertainment: Gifts', 138: 'Shopping & Entertainment: Hobbies', 139: 'Shopping & Entertainment: Online Services', 140: 'Transportation: Automotive Expenses', 141: 'Transportation: Car Payments', 142: 'Transportation: Gasoline/Fuel', 143: 'Transportation: Public Transportation', 144: 'Travel: Travel', 147: 'Income: Consulting', 148: 'Income: Deposits', 149: 'Income: Expense Reimbursement', 150: 'Income: Interest', 151: 'Income: Investment Income', 152: 'Income: Other Income', 153: 'Income: Paychecks/Salary', 154: 'Income: Retirement Income', 155: 'Income: Sales', 156: 'Income: Services', 157: 'Income: Wages Paid', 158: 'Finance: Bank of America Credit Card Payment', 159: 'Health: Fitness or Health club membership', 160: 'Insurance: Insurance', 161: 'Finance: Investment Account Fees/Charges', 999: 'Uncategorized: Uncategorized'}
        self.category_map[998] = None # Uncategorized: Pending

    def _get_balance(self, title):
        root = lxml.html.fromstring(self.html_data)
        for el in root.getiterator():
            if el.text and title in el.text:
                balance = el.getnext().text_content()
                return parse_amount(balance)
        return None

    def _get_transactions(self, next_link_xpath):
        for html_data in self.walk_pages(next_link_xpath):
            yield from self.process_page(html_data)

    def walk_pages(self, next_link_xpath):
        html_data = self.html_data
        while True:
            yield html_data
            root = lxml.html.fromstring(html_data)
            next_link = root.xpath(next_link_xpath)
            if len(next_link) == 0:
                break
            url = self.base_url + next_link[0].attrib['href']
            html_data = self.browser.get(url).read()
            # open('tmp.html', 'wb').write(html_data)

    def txn_id_from_link(self, details_link):
        m = re.search('txn=([0-9a-f]+)', details_link)
        return m.group(1)

class BankOfAmericaDebit(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        headers = {
            'content-type': 'application/json',
        }
        self.browser = WebBrowser('https://secure.bankofamerica.com/', ['.bankofamerica.com', 'secure.bankofamerica.com'], headers)
        self.base_url = 'https://secure.bankofamerica.com'
        self.url = f'{self.base_url}/ogateway/addapi/v1/activity'
        self.html_data = self.get_activity()
        self.category_map = {100: 'Business Expenses: Business Miscellaneous', 101: 'Business Expenses: Dues & Subscriptions', 102: 'Business Expenses: Office Maintenance', 103: 'Business Expenses: Office Supplies', 104: 'Business Expenses: Postage & Shipping', 105: 'Business Expenses: Printing', 106: 'Education: Education', 107: 'Finance: Credit Card Payments', 108: 'Finance: Loans', 109: 'Finance: Service Charges/Fees', 110: 'Finance: Taxes', 111: 'Giving: Giving', 112: 'Groceries: Groceries', 113: 'Health: Healthcare/Medical', 114: 'Health: Insurance', 115: 'Home & Utilities: Cable/Satellite Services', 116: 'Home & Utilities: Home Improvement', 117: 'Home & Utilities: Home Maintenance', 118: 'Home & Utilities: Mortgages', 119: 'Home & Utilities: Rent', 120: 'Home & Utilities: Telephone Services', 121: 'Home & Utilities: Utilities', 122: 'Cash, Checks & Misc: ATM/Cash Withdrawals', 123: 'Cash, Checks & Misc: Checks', 124: 'Cash, Checks & Misc: Other Bills', 125: 'Cash, Checks & Misc: Other Expenses', 126: 'Personal & Family Care: Child/Dependent Expenses', 127: 'Personal & Family Care: Personal Care', 128: 'Personal & Family Care: Pets/Pet Care', 129: 'Restaurants & Dining: Restaurants/Dining', 130: 'Savings & Transfers: Savings', 131: 'Savings & Transfers: Securities Trades', 132: 'Savings & Transfers: Transfers', 133: 'Shopping & Entertainment: Clothing/Shoes', 134: 'Shopping & Entertainment: Electronics', 135: 'Shopping & Entertainment: Entertainment', 136: 'Shopping & Entertainment: General Merchandise', 137: 'Shopping & Entertainment: Gifts', 138: 'Shopping & Entertainment: Hobbies', 139: 'Shopping & Entertainment: Online Services', 140: 'Transportation: Automotive Expenses', 141: 'Transportation: Car Payments', 142: 'Transportation: Gasoline/Fuel', 143: 'Transportation: Public Transportation', 144: 'Travel: Travel', 147: 'Income: Consulting', 148: 'Income: Deposits', 149: 'Income: Expense Reimbursement', 150: 'Income: Interest', 151: 'Income: Investment Income', 152: 'Income: Other Income', 153: 'Income: Paychecks/Salary', 154: 'Income: Retirement Income', 155: 'Income: Sales', 156: 'Income: Services', 157: 'Income: Wages Paid', 158: 'Finance: Bank of America Credit Card Payment', 159: 'Health: Fitness or Health club membership', 160: 'Insurance: Insurance', 161: 'Finance: Investment Account Fees/Charges', 999: 'Uncategorized: Uncategorized'}
        self.category_map[998] = None # Uncategorized: Pending

    def get_activity(self, next_token=None):
        req_data = {
            'payload': {
                'accountToken': self.account_id,
            },
            'pagingRules': {
                'pagingRequestedItemCount': 50,
            },
        }
        if next_token is not None:
            req_data['pagingRules']['pagingStartingItemToken'] = next_token
        req_data = json.dumps(req_data)
        data = self.browser.get(self.url, req_data.encode()).read()
        # open('tmp.html', 'wb').write(data)
        # data = open('tmp.html', 'rb').read()
        return json.loads(data, parse_float=decimal.Decimal)

    def get_balance(self):
        return self.html_data['payload']['depositActivity']['summary']['account']['availableBalance']['amount']

    def get_transactions(self):
        while True:
            yield from self.process_page(self.html_data)
            next_token = self.html_data['pagingRules'].get('pagingNextPageItemToken', None)
            if next_token is None:
                break
            self.html_data = self.get_activity(next_token)

    def process_page(self, html_data):
        for record in html_data['payload']['depositActivity']['transactionList']['transactions']:
            txn = Transaction()
            txn.account_name = self.nickname
            txn.date = datetime.datetime.fromtimestamp(record['postedTimestamp'] / 1000).date()
            txn.amount = decimal.Decimal(record['amount']['amount'])
            txn.bank_txn_id = create_hash(record['preferredDescription'], txn.date, txn.amount)

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                assert existing.matches(txn)
            else:
                details = self.download_transaction(record['transactionToken'])
                assert txn.date == details['date']
                assert txn.amount == details['amount']
                txn.description = details['description']
                txn.category = details['category']
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

    def download_transaction(self, txn_token):
        req_data = {
            'payload': {
                'transactionToken': txn_token,
                'accountToken': self.account_id,
            },
        }
        req_data = json.dumps(req_data)
        data = self.browser.get(f'{self.base_url}/ogateway/addapi/v1/transaction/detail/content', req_data.encode()).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)
        data = data['payload']['transaction']

        assert data['amount']['displayAmount'] == data['postedAmount']['displayAmount']
        if data['longDescription'] != data['shortDescription']:
            logging.debug('long description differs')
            logging.debug('%s', data['shortDescription'])
            logging.debug('%s', data['longDescription'])

        return {
            'date': datetime.datetime.utcfromtimestamp(data['postedTimestamp'] / 1000).date(),
            'amount': decimal.Decimal(data['postedAmount']['amount']),
            'description': data['longDescription'],
            'category': self.category_map[int(data['category']['code'])],
        }

class BankOfAmericaCredit(BankOfAmerica):
    def __init__(self, conn, nickname, account_id):
        super().__init__(conn, nickname, account_id)

    def get_balance(self):
        return self._get_balance('Current balance')

    def get_transactions(self):
        yield from self._get_transactions('//a[@name="goto_previous_transactions_top"]')

    def process_page(self, html_data):
        root = lxml.html.fromstring(html_data)
        for record in root.xpath('//table[@id="transactions"]//tr'):
            amount = record.xpath('.//td[4]')
            if len(amount) == 0:
                # TODO: why is this needed?
                continue

            txn = Transaction()
            txn.account_name = self.nickname
            txn.amount = parse_amount(amount[0].text)
            details_link = record.xpath('.//img[1]')[0].attrib['rel']
            txn.bank_txn_id = self.txn_id_from_link(details_link)
            date = list(record.xpath('.//td[1]')[0].itertext())[-1].strip()
            if len(date) == 0:
                # skip pending transactions
                continue
            posted_date = datetime.datetime.strptime(date, '%m/%d/%Y').date()
            txn.description = record.xpath('.//td[2]/a')[0].getchildren()[0].tail.rstrip()

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                assert existing.matches(txn)
                assert posted_date >= existing.date
            else:
                details = self.download_transaction(details_link)
                assert txn.bank_txn_id == details['bank_txn_id']
                assert posted_date >= details['date']
                # assert description.startswith(details['description'])
                assert details['category'] == self.category_map[details['category_id']]
                txn.date = details['date']
                txn.category = details['category']
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

    def download_transaction(self, details_link):
        data = self.browser.get(self.base_url + details_link).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        data = data.decode('ascii')
        root = lxml.html.fromstring(data)

        bank_txn_id = root.xpath('//table')[0].attrib['rel']
        category = root.xpath('//*[@class="lblCategoryName"]')[0].text

        data = {}
        for tr in root.xpath('//tr'):
            name = tr.xpath('.//*[contains(@class,"first-expanded-cell")]')
            if len(name) == 0:
                continue
            name = name[0].text
            if name is None:
                name = tr.xpath('.//span')[0].text.strip()
                value = tr.xpath('.//*[contains(@class,"second-expanded-cell")]/span/span')[0].text.strip()
            else:
                value = tr.xpath('.//*[contains(@class,"second-expanded-cell")]')[0].text.strip()
            name = name[:-1]
            data[name] = value

        return {
            'date': datetime.datetime.strptime(data['Transaction date'], '%m/%d/%Y').date(),
            'description': data['Merchant Name'],
            'category_id': int(data['Transaction Category']),
            'category': category,
            'bank_txn_id': bank_txn_id,
        }

class Chase(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        headers = {
            'x-jpmc-csrf-token': 'NONE',
        }
        self.browser = WebBrowser('https://secure05b.chase.com/web/auth/dashboard', ['.chase.com', 'secure05b.chase.com'], headers)
        url = 'https://secure05b.chase.com/svc/rr/accounts/secure/v4/activity/dda/list'
        self.data = self.browser.get(url, f'accountId={account_id}'.encode('ascii')).read()
        # open('tmp.html', 'wb').write(self.data)
        # self.data = open('tmp.html', 'rb').read()
        self.data = json.loads(self.data, parse_float=decimal.Decimal)
        self.category_map = {'ACCT_XFER': 'Account transfer', 'ACH_COLLECTION': 'ACH collection', 'ACH_CREDIT': 'ACH credit', 'ACH_DEBIT': 'ACH debit', 'ACH_PAYMENT': 'ACH vendor payment', 'ADJUSTMENT': 'Adjustment/reversal', 'ADJUSTMT_REVERSAL': 'Adjustment/reversal', 'ADVANCE_LOAN': 'Advances', 'ALL': 'All transactions', 'ALL_CREDIT': 'All credit transactions', 'ALL_DEBIT': 'All debit transactions', 'ALL_PENDING_TRANSACTIONS': 'All pending transactions', 'ALL_SETTLED_TRANSACTIONS': 'All settled transactions', 'ALL_TRANSACTIONS': 'All transactions', 'ANTICIPATED_INCOME': 'Anticipated income', 'ATM': 'ATM transaction', 'ATM_DEPOSIT': 'ATM deposit', 'AUTHORIZATION': 'Authorization', 'BANKING_BILL_PAYMENTS': 'Banking bill payments', 'BANKING_CREDITS': 'Banking credits', 'BANKING_DEBITS': 'Banking debits', 'BANKING_DEPOSITS': 'Banking deposits', 'BANKING_WIRES': 'Banking wires', 'BASIC_PAYROLL': 'ACH employee payment', 'BILLPAY': 'Bill payment', 'BILL_PAYMENT': 'Bill payment', 'CASH_ADVANCE': 'Cash advance', 'CHARGE': 'Charge', 'CHARGE_OFF': 'Charge off', 'CHASE_TO_PARTNERFI': 'QuickPay debit', 'CHECK': 'Checks under 2 years', 'CHECKS': 'Checks under 2 years', 'CHECK_DEPOSIT': 'Deposit', 'CHECK_PAID': 'Check', 'CHECK_PAID_CDA': 'Older checks', 'CHECK_RETURN': 'Adjustment/reversal', 'COMMITMENT_AMOUNT_DECREASE': 'Commitment decrease', 'COMMITMENT_AMOUNT_INCREASE': 'Commitment increase', 'DEBIT_CARD': 'Card', 'DEBIT_REVERSAL': 'QuickPay credit', 'DEPOSIT': 'Deposit', 'DEPOSIT_RETURN': 'Returned deposit item', 'DIVIDENDS_AND_INTEREST': 'Dividends &amp; interest', 'ESCROW_TRANSACTIONS': 'Escrow', 'EGIFT_DEBIT': 'eGift Debit', 'FEE': 'Fee', 'FEE_PAYMENTS': 'Fee payments', 'FEE_TRANSACTION': 'Fee', 'FX_TRADES': 'FX trades', 'INCOMING_WIRE_TRANSFER': 'Incoming wire transfer', 'INTRADAY_TRADES': 'Intraday trades', 'INVESTMENT_DEPOSITS_AND_WITHDRAWALS': 'Investment deposits &amp; withdrawals', 'INVESTMENT_FEES': 'Investment fees', 'LAST_STATEMENT': 'statement', 'LATE_TRANSACTION_CHARGES': 'Late Charges', 'LINES_OF_CREDIT': 'Line of credit', 'LOAN_PAYMENT': 'Payment', 'LOAN_PAYMENTS': 'Loan payments', 'LOAN_PMT': 'Loan payment', 'MISCELLANEOUS_TRANSACTIONS': 'Misc Transactions', 'MISCELLENEOUS_FEES_AND_EXPENSES': 'Misc Fees and Expenses', 'MISC_CREDIT': 'Misc. credit', 'MISC_DEBIT': 'Misc. debit', 'NON_TRADES': 'Non-trades', 'NSF_DEBIT': 'Misc. debit', 'OTHER_TRANSACTIONS': 'Other transactions', 'OUTGOING_WIRE_TRANSFER': 'Outgoing wire transfer', 'OVERNIGHT_CHECK': 'Overnight check', 'PARTNERFI_TO_CHASE': 'QuickPay credit', 'PAYMENT': 'Payment', 'PAYMENTS': 'Payments', 'PAY_WITH_POINTS': '', 'PENDING_NON_TRADES': 'Pending non-trades', 'PENDING_TRADES': 'Pending trades', 'PREMIUM_PAYROLL': 'Premium Payroll', 'PREPAID_CARD': 'Prepaid card transaction', 'PREPAID_CARDS': '', 'QUICKPAY_CREDIT': 'QuickPay credit', 'QUICKPAY_DEBIT': 'QuickPay debit', 'QUICK_DEPOSIT': 'Deposit', 'REFUND': 'Refund', 'REFUND_TRANSACTION': 'Refund', 'REPRICE': 'Repricing', 'RETURN': 'Return', 'RETURNED_DEPOSIT': 'Returned deposit item', 'RETURNS_AND_REVERSALS': 'Returns and Reversals', 'REVERSAL': 'Reversal', 'SALE': 'Sale', 'SECURITY_TRANSACTIONS': 'Security transfers', 'SINCE_LAST_STATEMENT': 'Since last statement', 'SWEEPS': 'Sweeps', 'TAX_PAYMENT': 'Tax payment', 'THREE_STATEMENTS_PRIOR': 'statement', 'TRADES': 'Trades', 'TRADES_WITHOUT_SWEEPS': 'Trades without sweeps', 'TRANSACTIONS_WITHOUT_SWEEPS': 'Transactions without sweeps', 'TWO_STATEMENTS_PRIOR': 'statement', 'WIRE_INCOMING': 'Incoming wire transfer', 'WIRE_ONLINE': 'Online wire transfer', 'WIRE_OUTGOING': 'Outgoing wire transfer', 'BUY': 'Buy', 'CASH': 'Cash', 'DIVIDENDS_INTEREST_FEES': 'Dividends/ Interest/ Fees', 'PENDING': 'Pending', 'SELL': 'Sell', 'TRANSFER': 'Transfer', 'CANCEL': 'Cancel', 'OTHER': 'Other', 'ACCOUNT_FEES': 'Account Fees', 'ACCOUNT_TRANSFER': 'Account transfer', 'ATM_CASH': 'ATM/ Cash', 'AUTOMOTIVE': 'Automotive', 'BILLS_UTILITIES': 'Bills/ Utilities', 'BUSINESS_MISC': 'Business Misc.', 'CHILD_DEPENDENTS': 'Child/ Dependents', 'EDUCATION': 'Education', 'ENTERTAINMENT': 'Entertainment', 'FASHION': 'Fashion', 'FOOD_AND_BEVERAGE': 'Food and Beverage', 'GENERAL_MERCHANDISE': 'General Merchandise', 'GIFTS_DONATIONS': 'Gifts/ Donations', 'HEALTHCARE_MEDICAL': 'Healthcare/ Medical', 'HOME_IMPROVEMENT': 'Home Improvement', 'INSURANCE': 'Insurance', 'LOANS': 'Loans', 'MISCELLANEOUS_SERVICES': 'Miscellaneous Services', 'MORTGAGE': 'Mortgage', 'OTHER_INCOME': 'Other Income', 'PAYCHECKS_SALARY': 'Paychecks/ Salary', 'PERSONAL_CARE': 'Personal Care', 'PETS_PET_CARE': 'Pets/ Pet Care', 'RENT': 'Rent', 'TAXES': 'Taxes', 'TRAVEL': 'Travel', 'UNCATEGORIZED': 'Uncategorized', 'EQUITY': 'Equity', 'FIXED_INCOME': 'Fixed income', 'MULTI_ASSET': 'Multi Asset', 'DERIVATIVE': 'Derivative', 'CASH_AND_CASH_EQUIVALENT': 'Cash and cash Equivalent', 'CURRENCY': 'Currency', 'COMMODITIES': 'Commodities', 'ALTERNATIVE': 'Alternative'}

    def get_balance(self):
        return decimal.Decimal(self.data['presentBalance'])

    def get_transactions(self):
        for activity in self.data['activities']:
            txn = Transaction()
            txn.account_name = self.nickname
            txn.date = datetime.datetime.strptime(activity['activityDate'], '%Y%m%d').date()
            txn.category = self.category_map[activity['activityTypeGroupFilter']]
            txn.description = activity['description']
            txn.amount = decimal.Decimal(activity['amount'])
            txn.bank_txn_id = activity['transactionId']

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                assert existing.matches(txn)
            else:
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

class WellsFargo(Bank):
    def __init__(self, conn, nickname, account_id):
        # TODO: code duplication
        self.conn = conn
        self.nickname = nickname
        self.base_url = 'https://connect.secure.wellsfargo.com'
        self.browser = WebBrowser(f'{self.base_url}/accounts/inquiry/summary/default?_x=plCAXPZydBh4UCBVW8je2GWeWqwwnhLY', ['.wellsfargo.com', '.secure.wellsfargo.com', '.connect.secure.wellsfargo.com', 'connect.secure.wellsfargo.com'])

        data = self.browser.get('https://connect.secure.wellsfargo.com/accounts/start').read()
        # open('tmp0.html', 'wb').write(data)
        # data = open('tmp0.html', 'rb').read()
        root = lxml.html.fromstring(data)
        url = self.base_url + root.xpath('//*[contains(@class,"account-title-group")]')[0].attrib['data-url']

        self.data = self.browser.get(url).read()
        # open('tmp.html', 'wb').write(self.data)
        # self.data = open('tmp.html', 'rb').read()
        self.data = self.parse_response(self.data)

    def get_balance(self):
        root = lxml.html.fromstring(self.data)
        for el in root.getiterator():
            if el.text and 'Current posted balance' in el.text:
                balance = el.getparent().getnext().text_content()
                return parse_amount(balance)
        return None

    def get_transactions(self):
        root = lxml.html.fromstring(self.data)
        start = False
        for record in root.xpath('//table[contains(@class,"transaction-expand-collapse")]//tr'):
            if start:
                details_link = record.xpath('.//td[1]/a')
                if len(details_link) == 0:
                    continue
                details_link = details_link[0].attrib['data-url']

                txn = Transaction()
                txn.account_name = self.nickname
                txn.date = datetime.datetime.strptime(record.xpath('.//td[2]/span')[0].text, '%m/%d/%y').date()
                txn.description = record.xpath('.//td[3]/span')[0].text
                amount = record.xpath('.//td[4]/span')
                mult = 1
                if len(amount) == 0:
                    mult = -1
                    amount = record.xpath('.//td[5]/span')
                txn.amount = mult * parse_amount(amount[0].text)
                # TODO: get the ending balance as well in the bank_txn_id
                txn.bank_txn_id = create_hash(txn.description, txn.date, txn.amount)

                existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
                if existing:
                    assert existing.matches(txn)
                else:
                    details = self.download_transaction(details_link)
                    txn.category = details['category']
                    txn.save(self.conn)

                yield ParsedTransaction(existing is None, txn)

            if 'Posted Transactions' in record.text_content():
                start = True

    def download_transaction(self, details_link):
        data = self.browser.get(self.base_url + details_link).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        data = self.parse_response(data)
        root = lxml.html.fromstring(data)

        return {
            'category': root.xpath('//span[@class="OneLinkNoTx"]')[0].text,
        }

    def parse_response(self, response):
        response = response[response.index(b'{'):response.rindex(b'}')+1]
        response = json.loads(response)
        response = html.unescape(response['htmlResponse'])
        return response

class Ally(Bank):
    '''
    // ==UserScript==
    // @name         New Userscript
    // @namespace    http://tampermonkey.net/
    // @version      0.1
    // @description  try to take over the world!
    // @author       You
    // @match        https://secure.ally.com/*
    // @icon         data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==
    // @grant        GM_setClipboard
    // ==/UserScript==

    (function() {
        'use strict';

        GM_setClipboard('not ready\n');
        (function(open) {
            XMLHttpRequest.prototype.open = function() {
                this.addEventListener("load", function() {
                    try {
                        var y = JSON.parse(atob(JSON.parse(this.responseText).data.data.json_data.token.split('.')[1]))['CIAM-App-Token'];
                        GM_setClipboard(`${y.CSRFChallengeToken},${y.AwsAccessToken}\n`);
                    } catch {
                    }
                }, false);
                open.apply(this, arguments);
            };
        })(XMLHttpRequest.prototype.open);
    })();
    '''

    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        self.walk_time_fmt = '%Y-%m-%d'
        self.browser = WebBrowser('https://secure.ally.com/dashboard', ['secure.ally.com', '.secure.ally.com', '.ally.com'])

        while True:
            tokens = input('enter tokens: ')
            if tokens != 'not ready':
                break
        csrf, token = tokens.strip().split(',')
        self.browser.headers = {
            'CSRFChallengeToken': csrf,
            'Authorization': 'Bearer ' + token,
        }

    def get_balance(self):
        url = f'https://secure.ally.com/capi-gw/accounts/{self.account_id}?include=accountAddress'
        data = self.browser.get(url).read()
        # open('tmp.html', 'wb').write(data)
        # data = open('tmp.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)
        return decimal.Decimal(data['sda']['currentBalancePvtEncrypt'])

    def walk_pages(self, from_date, to_date):
        url = f'https://secure.ally.com/capi-gw/accounts/{self.account_id}/transactions?fromDate={from_date}&toDate={to_date}'
        data = self.browser.get(url).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        yield data

    def process_page(self, data):
        data = json.loads(data, parse_float=decimal.Decimal)

        if 'transaction' not in data:
            return

        for t in data['transaction']:
            txn = Transaction()
            txn.account_name = self.nickname
            txn.bank_txn_id = t['transactionSequenceNumber']
            txn.date = datetime.datetime.strptime(t['transactionPostingDate'][:10], '%Y-%m-%d').date()
            txn.amount = t['transactionAmountPvtEncrypt']
            txn.description = t['transactionDescription']
            txn.category = None
            # TODO: category

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                assert existing.matches(txn)
            else:
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

class Marcus(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        self.walk_time_fmt = '%Y-%m-%d'
        headers = {
            'content-type': 'application/json',
        }
        self.browser = WebBrowser(f'https://www.marcus.com/us/en/your-savings/account-detail?accountId={account_id}', ['marcus.com', '.marcus.com', '.api.marcus.com', 'api.marcus.com'], headers)
        self.url = 'https://api.marcus.com/cos/'

    def get_balance(self):
        req_data = {
            'operationName':'savingsAccountDetailSavingsAccount',
            'variables':{
                'parameters':f'{{"accountId":"{self.account_id}"}}',
                'queryString':'?includeClosureEligibility=true&includeMaturityPlanEligibility=true'
            },
            'query':'query savingsAccountDetailSavingsAccount($parameters: String, $queryString: String) {\n  data(parameters: $parameters, queryString: $queryString) {\n    savingsAccount {\n      error {\n        code\n        message\n        __typename\n      }\n      response {\n        accountCloseIneligibleReason\n        accountName\n        accountNumber\n        accountNumberLastFour\n        annualYield\n        availableBalance\n        accruedInterest\n        accountNumberLastFour\n        balance\n        cdMaturityDate\n        cdTerm\n        dateOpened\n        eligibleForAccountClose\n        eligibleToSetMaturityPlan\n        interestYearToDate\n        promotionCode\n        promoEnrollmentDate\n        promotionDetails {\n          hasActivePromotion\n          enrollmentDetails {\n            promoEnrollmentId\n            promotionCode\n            promoStatus\n            promoEnrollmentStatusUpdatedOn\n            __typename\n          }\n          __typename\n        }\n        transactionsForCurrentBillingPeriod\n        type\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}\n'
        }
        req_data = json.dumps(req_data)
        data = self.browser.get(self.url, req_data.encode('ascii')).read()
        # open('tmp.html', 'wb').write(data)
        # data = open('tmp.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)
        return decimal.Decimal(data['data']['data']['savingsAccount']['response']['balance'])

    def walk_pages(self, from_date, to_date):
        req_data = {
            'operationName':'savingsAccountDetailSavingsPostedActivities',
            'variables':{
                'parameters':f'{{"accountId":"{self.account_id}"}}',
                'queryString':f'?activityType=POSTED&endDate={to_date}&startDate={from_date}&transactionType=ALL&includeStatements=false'
            },
            'query':'query savingsAccountDetailSavingsPostedActivities($parameters: String, $queryString: String) {\n  data(parameters: $parameters, queryString: $queryString) {\n    savingsAccountsActivities {\n      response {\n        posted {\n          achReturnCode\n          description\n          activityType\n          postedDate\n          amount\n          endingBalance\n          fromAccountName\n          fromAccountNumberLastFour\n          isReversal\n          toAccountName\n          toAccountNumberLastFour\n          transactionCode\n          wireTransferCompletionDate\n          wireTransferStatus\n          wireTransferSubmittedDate\n          xferCreatedDate\n          xferEffectiveDate\n          xferEstimatedProcessDate\n          xferFundsAvailableDate\n          __typename\n        }\n        __typename\n      }\n      error {\n        code\n        message\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}\n'
        }
        req_data = json.dumps(req_data)
        data = self.browser.get(self.url, req_data.encode('ascii')).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        yield data

    def process_page(self, data):
        data = json.loads(data, parse_float=decimal.Decimal)

        for activity in data['data']['data']['savingsAccountsActivities']['response']['posted']:
            txn = Transaction()
            txn.account_name = self.nickname
            txn.date = datetime.datetime.strptime(activity['postedDate'], '%Y-%m-%d').date()
            txn.amount = activity['amount']
            txn.description = activity['description']
            txn.category = None
            txn.bank_txn_id = create_hash(txn.description, activity['postedDate'], txn.amount, activity['endingBalance'])

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                assert existing.matches(txn)
            else:
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

class AmericanExpress(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        headers = {
            'account_tokens': account_id,
        }
        self.browser = WebBrowser('https://global.americanexpress.com/activity/recent', ['global.americanexpress.com', '.global.americanexpress.com', 'americanexpress.com', '.americanexpress.com'], headers)

    def get_balance(self):
        url = 'https://global.americanexpress.com/api/servicing/v1/financials/balances?extended_details=deferred,non_deferred,pay_in_full,pay_over_time,early_pay'
        data = self.browser.get(url).read()
        # open('tmp.html', 'wb').write(data)
        # data = open('tmp.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)
        assert data[0]['account_token'] == self.account_id
        return decimal.Decimal(data[0]['statement_balance_amount'])

    def get_transactions(self):
        url = 'https://global.americanexpress.com/api/servicing/v1/financials/statement_periods'
        data = self.browser.get(url).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        data = json.loads(data)
        for statement in data:
            end_date = statement['statement_end_date']
            url = f'https://global.americanexpress.com/api/servicing/v1/financials/transactions?limit=1000&statement_end_date={end_date}&status=posted&extended_details=merchant,category,tags,rewards,offer'
            data = self.browser.get(url).read()
            # open('tmp3.html', 'wb').write(data)
            # data = open('tmp3.html', 'rb').read()
            data = json.loads(data, parse_float=decimal.Decimal)
            for t in data['transactions']:
                txn = Transaction()
                txn.account_name = self.nickname
                txn.bank_txn_id = t['identifier']
                txn.date = datetime.datetime.strptime(t['charge_date'], '%Y-%m-%d').date()
                txn.amount = t['amount']
                txn.description = t['description']
                txn.category = None
                if 'extended_details' in t and 'category' in t['extended_details']:
                    txn.category = t['extended_details']['category']['category_name'] + ' - ' + t['extended_details']['category']['subcategory_name']

                existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
                if existing:
                    assert existing.matches(txn)
                else:
                    txn.save(self.conn)

                yield ParsedTransaction(existing is None, txn)

class FidelityCredit(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        self.walk_time_fmt = '%m-%d-%Y'
        self.browser = WebBrowser('https://login.fidelityrewards.com/onlineCard/transactionDetails.do', ['login.fidelityrewards.com', '.login.fidelityrewards.com', 'fidelityrewards.com', '.fidelityrewards.com'])

        timestamp = int(datetime.datetime.now().timestamp() * 1000)
        url = f'https://login.fidelityrewards.com/onlineCard/public/publicAppInfo.action?host=login.fidelityrewards.com&timestamp={timestamp}'
        data = self.browser.get(url).read()
        # open('tmp.html', 'wb').write(data)
        # data = open('tmp.html', 'rb').read()
        data = json.loads(data)
        self.csrf = data['csrf-token']

    def get_balance(self):
        timestamp = int(datetime.datetime.now().timestamp() * 1000)

        params = {
            'timestamp': timestamp,
            'partner': 'fid',
            'userId': self.account_id,
            'CSRFToken': self.csrf,
        }
        params = urllib.parse.urlencode(params).encode('ascii')

        url = f'https://login.fidelityrewards.com/onlineCard/transactionDetails.action?timestamp={timestamp}'

        data = self.browser.get(url, params).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)
        return decimal.Decimal(data['currentBalance'])

    def walk_pages(self, from_date, to_date):
        page = 0

        while True:
            page += 1
            timestamp = int(datetime.datetime.now().timestamp() * 1000)

            params = {
                'timestamp': timestamp,
                'partner': 'fid',
                'userId': self.account_id,
                'CSRFToken': self.csrf,
                'transactionsType': '04',
                'phase': 'display',
                'state': page,
                'pendingState': '1',
                'postedState': '1',
                'recurringState': '1',
                'lastRefineTransState': '',
                'sortProperty': '',
                'beginDate': from_date,
                'endDate': to_date,
            }

            params = urllib.parse.urlencode(params).encode('ascii')

            url = f'https://login.fidelityrewards.com/onlineCard/transactionDetails.action?phase=display&timestamp={timestamp}'

            data = self.browser.get(url, params).read()
            # open('tmp3.html', 'wb').write(data)
            # data = open('tmp3.html', 'rb').read()
            yield data
            data = json.loads(data, parse_float=decimal.Decimal)
            if data['viewingLastItem']:
                break

    def process_page(self, data):
        data = json.loads(data, parse_float=decimal.Decimal)
        for t in data['postedTransactions']:
            txn = Transaction()
            txn.account_name = self.nickname
            txn.bank_txn_id = t['transTimestamp']
            _, month, day, _, _, year = t['tDate'].split()
            txn.date = datetime.datetime.strptime(f'{year} {month} {day}', '%Y %b %d').date()
            txn.amount = t['amount']
            txn.description = t['description']

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                if existing.description != txn.description:
                    logging.debug('fidelity changed description %r to %r', existing.description, txn.description)
                    existing.description = txn.description
                assert existing.matches(txn)
            else:
                details = self.download_transaction(txn.bank_txn_id)
                txn.category = details['category']
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

    def download_transaction(self, bank_txn_id):
        timestamp = int(datetime.datetime.now().timestamp() * 1000)

        params = {
            'timestamp': timestamp,
            'partner': 'fid',
            'userId': self.account_id,
            'CSRFToken': self.csrf,
            'transactionsType': '04',
            'transTimestamp': bank_txn_id,
        }

        params = urllib.parse.urlencode(params).encode('ascii')

        url = f'https://login.fidelityrewards.com/onlineCard/enhancedTransactionDetails.action?timestamp={timestamp}'

        data = self.browser.get(url, params).read()
        # open('tmp4.html', 'wb').write(data)
        # data = open('tmp4.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)

        return {
            'category': data.get('mccDescription', ''),
        }

class Fidelity(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        self.walk_time_fmt = '%m/%d/%Y'
        self.browser = WebBrowser('https://oltx.fidelity.com/ftgw/fbc/oftop/portfolio', ['oltx.fidelity.com', '.oltx.fidelity.com', 'fidelity.com', '.fidelity.com'])

        data = self.browser.get('https://oltx.fidelity.com/ftgw/fbc/oftop/portfolio').read()
        # open('tmp.html', 'wb').write(data)
        # data = open('tmp.html', 'rb').read()
        root = lxml.html.fromstring(data)
        self.csrf = root.xpath('//input[@class="account-mini-context"]')[0].attrib['value']

    def get_balance(self):
        params = {
            'accountNumber': self.account_id,
            'accountType': 'Brokerage',
            'isMultiMrgnSummaryView': 'false',
            'systemOfRecord': '',
            'isBalancesLWCEnabled': 'true',
            'isAcccountLWCEnabled': 'false',
        }
        params = urllib.parse.urlencode(params).encode('ascii')

        data = self.browser.get('https://digital.fidelity.com/ftgw/digital/balances/api', params).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)
        assert data['account']['acctNum'] == self.account_id
        return decimal.Decimal(data['balance']['brokBalDetail']['cashDetail']['coreCash'])

    def walk_pages(self, from_date, to_date):
        params = {
            'accounts': self.account_id,
            'VIEW_TYPE': 'NON_CORE',
            'FROM_DATE': from_date,
            'TO_DATE': to_date,
            'PERIOD': '',
            'ACCT_HIST_DAYS': 'RANGE',
            'ACCT_HIST_SORT': 'DATE',
            'SORT_TYPE': 'D',
            'CSV': '',
            'SECURITY_TYPE': 'Symbol',
            'SECURITY_VAL': '',
            'TYPE': 'ALL_TRANSACTIONS',
            'amc': self.csrf,
        }

        params = urllib.parse.urlencode(params).encode('ascii')

        data = self.browser.get('https://digital.fidelity.com/ftgw/digital/acct-activity/activity-tab-history/api', params).read()
        # open('tmp3.html', 'wb').write(data)
        # data = open('tmp3.html', 'rb').read()
        yield data

    def process_page(self, data):
        data = json.loads(data, parse_float=decimal.Decimal)

        if 'txnDetail' not in data['transaction']['txnDetails']:
            return

        for t in data['transaction']['txnDetails']['txnDetail']:
            assert t['acctNum'] == self.account_id
            assert t['autoTxnDesc'] == t['txnDescription']

            if t['txnDescription'].startswith('REINVESTMENT CASH'):
                logging.debug('skip txn %s', t['txnDescription'])
                continue

            txn = Transaction()
            txn.account_name = self.nickname
            txn.date = datetime.datetime.strptime(t['date'], '%m/%d/%Y').date()
            if t['amount'] == '--':
                txn.amount = 0
            else:
                txn.amount = parse_amount(t['amount'])
            txn.description = t['txnDescription']
            txn.category = None
            txn.bank_txn_id = create_hash(txn.description, t['postedDate'], txn.amount, t['cashBalance'], t['amtDetail']['shares'])

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                assert existing.matches(txn)
            else:
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

class FirstTechFed(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        headers = {
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
        }
        self.browser = WebBrowser('https://banking.firsttechfed.com/MyAccountsV2', ['banking.firsttechfed.com', '.banking.firsttechfed.com', '.firsttechfed.com', 'firsttechfed.com'], headers)

    def get_balance(self):
        url = f'https://banking.firsttechfed.com/MyAccountsV2/GetCurrentAccountBalance?accountIdentifier={self.account_id[1]}'
        data = self.browser.get(url).read()
        # open('tmp.html', 'wb').write(data)
        # data = open('tmp.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)
        return parse_amount(data['Balance'])

class FirstTechFedCsv(FirstTechFed):
    def __init__(self, conn, nickname, account_id):
        super().__init__(conn, nickname, account_id)
        self.walk_time_fmt = '%Y-%m-%d'

    def get_transactions(self):
        url = f'https://banking.firsttechfed.com/MyAccountsV2/Export?accountIdentifier={self.account_id[1]}'
        data = self.browser.get(url).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        root = lxml.html.fromstring(data)
        self.csrf = root.xpath('//input[@name="__RequestVerificationToken"]')[0].attrib['value']

        yield from super().get_transactions()

    def walk_pages(self, from_date, to_date):
        params = {
            '__RequestVerificationToken': self.csrf,
            'AccountIdentifiers': self.account_id[1],
            'Parameters.TransactionCategoryId': '',
            'Parameters.Debit': '',
            'Parameters.Description': '',
            'Parameters.MaximumAmount': '',
            'Parameters.MinimumAmount': '',
            'Parameters.TransactionTypeId': '',
            'format': '54',
            'Parameters.StartDate': from_date,
            'Parameters.EndDate': to_date,
        }
        params = urllib.parse.urlencode(params).encode('ascii')

        data = self.browser.get('https://banking.firsttechfed.com/MyAccountsV2/Export', params).read()
        # open('tmp3.html', 'wb').write(data)
        # data = open('tmp3.html', 'rb').read()
        data = json.loads(data)
        storage_token = data['result']['StorageToken']

        params = {
            'storageToken': storage_token,
        }
        params = urllib.parse.urlencode(params).encode('ascii')

        data = self.browser.get('https://banking.firsttechfed.com/MyAccountsV2/DownloadExportFile', params).read()
        # open('tmp4.html', 'wb').write(data)
        # data = open('tmp4.html', 'rb').read()
        yield data

    def process_page(self, data):
        csv_reader = csv.DictReader(io.StringIO(data.decode('ascii')))
        for row in csv_reader:
            assert ' %s ' % self.account_id[0] in row['Transaction ID']

            txn = Transaction()
            txn.account_name = self.nickname
            txn.bank_txn_id = row['Reference Number']
            txn.amount = decimal.Decimal(row['Amount'])
            txn.category = row['Transaction Category']
            # if len(txn.category) == 0:
            #     txn.category = row['Type']
            txn.date = datetime.datetime.strptime(row['Posting Date'], '%m/%d/%Y').date()
            txn.description = row['Description']

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                assert existing.matches(txn)
            else:
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

class FirstTechFedWeb(FirstTechFed):
    def get_transactions(self):
        page = -1
        start = False
        while True:
            page += 1

            more = '&isLoadingMore=true' if page > 0 else ''
            url = f'https://banking.firsttechfed.com/MyAccountsV2/Transactions?description=&sort=PostingDate&dir=desc&date=&start_date=&end_date=&category=&from_amount=&to_amount=&ranged_amount=&type=&credit_debit=&from_check_range=&to_check_range=&account_identifier={self.account_id[1]}&account_id={self.account_id[0]}&start={page}&limit=25{more}'
            data = self.browser.get(url).read()
            # open('tmp2.html', 'wb').write(data)
            # data = open('tmp2.html', 'rb').read()
            root = lxml.html.fromstring(data)

            if len(root.xpath('//div[@id="posted_transactions"]')) > 0:
                start = True
            if not start:
                continue
            transactions = root.xpath('//div[contains(@class,"transaction-row")]')

            for t in transactions:
                assert t.attrib['data-account-identifier'] == self.account_id[1]

                txn = Transaction()
                txn.account_name = self.nickname
                txn.bank_txn_id = t.attrib['data-transaction-id']
                txn.amount = decimal.Decimal(t.attrib['data-amount'])
                txn.category = t.attrib['data-selected-category']
                month = t.xpath('.//span[@class="month"]')[0].text
                day = t.xpath('.//span[@class="day"]')[0].text
                year = t.xpath('.//span[@class="year"]')[0].text
                txn.date = datetime.datetime.strptime(f'{year} {month} {day}', '%Y %b %d').date()
                txn.description = t.xpath('.//span[contains(@class,"description")]')[0].text.strip()

                existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
                if existing:
                    assert existing.matches(txn)
                else:
                    txn.save(self.conn)

                yield ParsedTransaction(existing is None, txn)

            if len(root.xpath('//div[contains(@class,"is-last-page")]')) > 0:
                break

class CapitalOne(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        self.walk_time_fmt = '%Y-%m-%d'
        headers = {
            'Accept': 'application/json;v=1',
        }
        self.browser = WebBrowser('https://myaccounts.capitalone.com/accountSummary', ['capitalone.com', '.capitalone.com', 'myaccounts.capitalone.com', '.myaccounts.capitalone.com'], headers)

    def get_balance(self):
        url = f'https://myaccounts.capitalone.com/ease-app-web/edge/Bank/accountdetail/getaccountbyid/{self.account_id}?productId=3800&productType=SA'
        data = self.browser.get(url).read()
        # open('tmp.html', 'wb').write(data)
        # data = open('tmp.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)
        return decimal.Decimal(data['accountDetails']['currentBalance'])

    def walk_pages(self, from_date, to_date):
        self.browser.headers = {
            'Accept': 'application/json;v=2',
        }
        url = f'https://myaccounts.capitalone.com/ease-app-web/edge/Bank/accounts/{self.account_id}/transactions?&startDate={from_date}&endDate={to_date}'
        data = self.browser.get(url).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        yield data

    def process_page(self, data):
        data = json.loads(data, parse_float=decimal.Decimal)

        if 'posted' not in data:
            return

        for activity in data['posted']:
            txn = Transaction()
            txn.account_name = self.nickname
            txn.date = datetime.datetime.strptime(activity['effectiveDate'].split('T')[0], '%Y-%m-%d').date()
            mult = -1 if activity['debitCardType'] == 'Debit' else 1
            txn.amount = mult * decimal.Decimal(activity['transactionTotalAmount'])
            txn.description = activity['statementDescription']
            txn.category = activity['transactionOverview']['category']
            txn.bank_txn_id = activity['transactionId']

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                assert existing.matches(txn)
            else:
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

class Citibank(Bank):
    def __init__(self, conn, nickname, account_id):
        self.conn = conn
        self.nickname = nickname
        self.account_id = account_id
        self.walk_time_fmt = '%Y-%m-%d'
        self.browser = self.create_browser(f'https://online.citi.com/US/ag/accountactivity/{self.account_id}')

    @classmethod
    def create_browser(cls, referer):
        browser = WebBrowser(referer, ['citi.com', '.citi.com', 'online.citi.com', '.online.citi.com'])
        auth_cookie = dict(c.split('=', 1) for c in browser.cookies['NGACoExistenceCookie'].split('|'))
        browser.headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + auth_cookie['authToken'],
            'client_id': auth_cookie['clientId'],
            'bizToken': auth_cookie['bizToken'],
        }
        return browser

    @classmethod
    def get_accounts(cls):
        browser = cls.create_browser('https://online.citi.com/US/ag/mrc/dashboard')
        url = 'https://online.citi.com/US/REST/nga/ngasessionmanagement.jws'
        req_data = {
            'coexistenceNeeded': 'N',
            'gemfirePushNeeded': 'Y',
        }
        req_data = json.dumps(req_data)
        data = browser.get(url, req_data.encode()).read()
        # open('tmp0.html', 'wb').write(data)
        # data = open('tmp0.html', 'rb').read()
        data = json.loads(data)
        for account in data['accounts']:
            yield Account(account['accountInstanceId'], account['completeDescription'])

    def get_balance(self):
        url = f'https://online.citi.com/gcgapi/prod/public/v1/v1/bank/accounts/{self.account_id}/detailsFromTPS/retrieve'
        data = self.browser.get(url, b'').read()
        # open('tmp.html', 'wb').write(data)
        # data = open('tmp.html', 'rb').read()
        data = json.loads(data, parse_float=decimal.Decimal)
        balance = data['accountDetails']['startOfDayBalance']
        if balance is None:
            return decimal.Decimal(0)
        return decimal.Decimal(balance)

    def walk_pages(self, from_date, to_date):
        url = f'https://online.citi.com/gcgapi/prod/public/v1/v1/digital/bankLedger/accounts/{self.account_id}/transactions/summaryAndBalances'
        req_data = {
            'timePeriodFilter': {
                'filterIndicator': 'DATE',
                'startRange': from_date,
                'endRange': to_date,
            },
        }
        req_data = json.dumps(req_data)
        data = self.browser.get(url, req_data.encode()).read()
        # open('tmp2.html', 'wb').write(data)
        # data = open('tmp2.html', 'rb').read()
        yield data

    def process_page(self, data):
        data = json.loads(data, parse_float=decimal.Decimal)

        def _get(activity, columnId, columnValue='actualValue', default=None):
            for column in activity['transactionColumns']:
                if column is not None and column['columnId'] == columnId:
                    return column[columnValue]
            return default

        for activity in data['accountActivity']['postedTransactions']:
            txn = Transaction()
            txn.account_name = self.nickname
            txn.date = datetime.datetime.strptime(_get(activity, 'DATE', 'displayValue'), '%m/%d/%Y').date()
            txn.amount = decimal.Decimal(_get(activity, 'CREDIT', default=0))
            txn.amount -= decimal.Decimal(_get(activity, 'DEBIT', default=0))
            txn.description = _get(activity, 'DESC') + activity['extendedDescriptions'][0]['displayValue']
            txn.category = None
            txn.bank_txn_id = activity['transactionId']

            existing = Transaction.load(self.conn, txn.account_name, txn.bank_txn_id)
            if existing:
                assert existing.matches(txn)
            else:
                txn.save(self.conn)

            yield ParsedTransaction(existing is None, txn)

def main():
    logging.basicConfig(level=logging.DEBUG)

    conn = sqlite3.connect('storage.sqlite')
    conn.row_factory = sqlite3.Row
    Transaction.create_table(conn)

    accounts = [
    ]

    banks = [
    ]

    for klass, mapping in banks:
        for account in klass.get_accounts():
            accounts.append((klass, mapping[account.name], account.id))

    for klass, account_name, *params in accounts:
        account = klass(conn, account_name, *params)
        balance = account.get_balance()
        print('balance', account_name, balance)
        for parsed_txn in account.get_transactions():
            txn = parsed_txn.txn
            if parsed_txn.new:
                print(txn.date, txn.amount, txn.description)

if __name__ == '__main__':
    main()
