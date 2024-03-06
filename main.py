import csv
from abc import ABC
from collections import namedtuple
from typing import Mapping

import aiohttp
import asyncio
from bs4 import BeautifulSoup
import time
from aioconsole import get_standard_streams
import unicodedata
from collections import Counter

# Read TSV file into a list of tuples
file_path = '/home/sirati/Vocab Unit 1 audio only.tsv'
# Write the updated data back to a TSV file
output_file_path = '/home/sirati/Vocab Unit 1 audio only hanja.tsv'
column_hangul = 1
column_english = 2

console_semaphore = asyncio.Semaphore(1)
reader, writer = None, None


def log(txt):
    async def perform_log():
        async with console_semaphore:
            print(txt)

    asyncio.ensure_future(perform_log())


async def sleep_till(timestamp):
    now = time.time()
    if timestamp > time.time() + 0.01:
        await asyncio.sleep(now - timestamp)


async def fetch_urls(list_args, map_to_url_func, process_response, session, none_value):
    log('Fetching urls...')
    _process_response = process_response
    def process_response (**kwargs): return asyncio.ensure_future(_process_response(**kwargs))
    def get (index): return asyncio.ensure_future(session.get(map_to_url_func(**list_args[index])))

    tasks = [None] * len(list_args)
    responses = [None] * len(list_args)
    start_time = time.time()
    had_error = False

    for index, args in enumerate(list_args):
        for i, task in filter(lambda x: x[1] is not None, enumerate(tasks[0:index - 1])):
            if task.done:
                tasks[i] = None
                response = await task
                if response.status == 200:
                    log(f'got good respone code {response.status} for URL {map_to_url_func(**list_args[i])}')
                    responses[i] = process_response(response=response, **list_args[i])
                else:
                    log(f'got bad respone code {response.status} for URL {map_to_url_func(**list_args[i])}')
                    had_error = True
                    break

        log(f'fetching URL {map_to_url_func(**list_args[index])}')
        tasks[index] = get(index)

    await asyncio.gather(*filter(lambda x: x is not None, tasks))
    end_time = time.time()

    for i, task in filter(lambda x: x[1] is not None, enumerate(tasks)):
        assert task.done
        tasks[i] = None
        response = await task
        if response.status == 200:
            responses[i] = process_response(response=response, **list_args[i])
        else:
            had_error = True

    if had_error:
        missing = [i for i, response in enumerate(responses) if response is None]
        missing.reverse()
        current = missing.pop()
        count_successful = len(responses) - len(missing)

        backoff = [0.1, 0.5, 1, 2, 5, 10, 30, 60, 120]
        backoff_index = 0
        while True:
            await sleep_till(end_time + backoff[backoff_index])
            if backoff_index < len(backoff):
                backoff_index += 1

            response = await get(current)
            if response.status == 200:
                responses[current] = process_response(response=response, **list_args[current])
                end_time = time.time()
                break
            else:
                log(f'at backoff {backoff[backoff_index]} got bad respone code {response.status} for URL {map_to_url_func(**list_args[current])}')

        wait_per_request = (end_time - start_time) / count_successful
        active_tasks = {}
        last_wait = end_time
        while True:
            current = missing.pop()
            active_tasks[current] = get(current)
            await sleep_till(last_wait + wait_per_request)
            last_wait = time.time()
            for i, task in active_tasks.items():
                if task.done:
                    response = await task
                    if response.status == 200:
                        responses[i] = process_response(response=response, **list_args[i])
                    else:
                        log(f'got bad respone code {response.status} for URL {map_to_url_func(**list_args[i])}')
                        missing.append(i)

    return [await response for response in responses]


class RateLimitedError(Exception):
    pass


def map_to_url(hangul, eng_word):
    return f'https://koreanhanja.app/{hangul}'

def clean_str(string):
    result = string.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ').strip()
    if result.endswith('(conjugate verb)'):
        result = clean_str(result[0:-len('(conjugate verb)')])
    elif result.startswith('a '):
        result = result[2:]
    elif result.startswith('an '):
        result = result[3:]
    elif result.startswith('one ') or result.startswith('the '):
        result = result[4:]

    return result


def normalise_cjk(items):
    def has_combat(string):
        for char in string:
            name = unicodedata.name(char, '')
            if name.startswith('CJK COMPATIBILITY IDEOGRAPH'):
                return True
        return False

    has_combats = [has_combat(hanja) for hanja, _ in items]
    for i, (hanja, eng) in enumerate(items):
        if has_combat(hanja):
            items[i] = (unicodedata.normalize('NFC', hanja), eng)

def remove_dup_hanja(items):
    count = Counter((hanja for hanja, _ in items))
    to_delete = []
    for i, (hanja, _) in enumerate(items):
        if count[hanja] > 1:
            count[hanja] -= 1
            to_delete.append(i)

    for i, index in enumerate(to_delete):
        del items[index - i]

read_semaphore = asyncio.Semaphore(1)
async def extract_similar_words(response, hangul, eng_word):
    async with response:
        assert response.status == 200
        html = await response.text()

    similar_words = []
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table', class_='similar-words')
    if table:
        rows = table.find_all('tr')
        for row in rows:
            tds = row.find_all('td')
            if len(tds) == 3 and clean_str(tds[1].text) == hangul:
                similar_words.append((clean_str(tds[0].text.strip()), clean_str(tds[2].text)))

    normalise_cjk(similar_words)
    remove_dup_hanja(similar_words)
    log(f'{hangul=}, {eng_word=}, {similar_words=}')

    if len(similar_words) == 1:
        return similar_words[0][0]
    elif len(similar_words) == 0:
        return None
    else:
        english_filtered = list(filter(lambda tuple: tuple[1].lower() == eng_word.lower(), similar_words))
        if len(english_filtered) == 0: pass
        elif (len(english_filtered) == 1 or
                (len(english_filtered) > 1 and len(set([hanja for hanja, eng in english_filtered])) == 1)):
            return english_filtered[0][0]

        async with console_semaphore:
            lookup = b'1234567890qwertyuiopadfghjklzxcvbnm'[0:len(similar_words)]
            lookup += b's'

            print(f'Multiple options for {hangul} and {eng_word}:')
            print('[s] for skipping')
            for i, similar_word in enumerate(similar_words):
                print(f'[{chr(lookup[i])}]: {similar_word}')

            async with read_semaphore:
                async def read_byte():
                    keys = await reader.read(1)
                    while len(keys) == 0:
                        await asyncio.sleep(0.1)
                        keys = await reader.read(1)
                    return keys[0]

                print('Input=', end='')
                key = await read_byte()

                while key not in lookup:
                    if key != b'\n'[0]:
                        print(' is Invalid input!\nInput=', end='')
                    key = await read_byte()

                if key == b's'[0]:
                    return
                else:
                    return similar_words[lookup.index(key)][0]


def read_tsv_file(file_path):
    # Read the TSV file into a list of tuples
    data = []
    with open(file_path, 'r', newline='', encoding='utf-8') as file:
        reader = csv.reader(file, delimiter='\t')
        for row in reader:
            if len(row) >= max(column_hangul, column_english) + 1:
                data.append(tuple(row))
    return data


def write_tsv_file(file_path, data):
    # Write the data list back to a TSV file
    with open(file_path, 'w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file, delimiter='\t')
        for row in data:
            writer.writerow(row)


def create_word_pairs(data):
    word_pairs = []
    for row in data:
        # Create WordPair instances using index 1 and 2 of each tuple
        word_pair = dict(hangul=row[column_hangul], eng_word=row[column_english])
        word_pairs.append(word_pair)
    return word_pairs


def add_column(data, column_data):
    # Append the column_data to the original data
    for i in range(len(data)):
        data[i] += (column_data[i],)
    return data


async def main():
    global reader
    global writer
    reader, writer = await get_standard_streams()

    data = read_tsv_file(file_path)
    # Create WordPair instances using index 1 and 2 of each tuple
    word_pairs = create_word_pairs(data)

    async with aiohttp.ClientSession() as session:
        hanja_column = await fetch_urls(word_pairs, map_to_url, extract_similar_words, session, none_value='')

    # Add column_data to the original data
    data_with_column = add_column(data, hanja_column)

    write_tsv_file(output_file_path, data_with_column)


def binary_search_count(arr, target):
    left, right = 0, len(arr) - 1
    while left <= right:
        mid = left + (right - left) // 2
        if arr[mid] <= target:
            left = mid + 1
        else:
            right = mid - 1
    return left


if __name__ == "__main__":
    asyncio.run(main())
