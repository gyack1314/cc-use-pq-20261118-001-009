from datetime import datetime, timezone


def merge(intervals):
    ordered = sorted((left, right) for left, right in intervals if left < right)
    result = []
    for left, right in ordered:
        if result and left <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], right))
        else:
            result.append((left, right))
    return result


def intersect(first, second):
    first, second = merge(first), merge(second)
    i = j = 0
    result = []
    while i < len(first) and j < len(second):
        left = max(first[i][0], second[j][0])
        right = min(first[i][1], second[j][1])
        if left < right:
            result.append((left, right))
        if first[i][1] <= second[j][1]:
            i += 1
        else:
            j += 1
    return result


def subtract(intervals, exclusions):
    intervals, exclusions = merge(intervals), merge(exclusions)
    result = []
    j = 0
    for left, right in intervals:
        while j < len(exclusions) and exclusions[j][1] <= left:
            j += 1
        cursor, k = left, j
        while k < len(exclusions) and exclusions[k][0] < right:
            a, b = exclusions[k]
            if cursor < a:
                result.append((cursor, min(a, right)))
            cursor = max(cursor, b)
            if cursor >= right:
                break
            k += 1
        if cursor < right:
            result.append((cursor, right))
    return result


def duration(intervals):
    return sum(right - left for left, right in merge(intervals))


def day_parts(intervals):
    result = {}
    for left, right in merge(intervals):
        cursor = left
        while cursor < right:
            day = cursor // 86400
            boundary = min((day + 1) * 86400, right)
            label = datetime.fromtimestamp(day * 86400, timezone.utc).date().isoformat()
            result[label] = result.get(label, 0) + boundary - cursor
            cursor = boundary
    return dict(sorted(result.items()))


def coverage(intervals, bounds):
    clipped = intersect(intervals, [bounds])
    total = bounds[1] - bounds[0]
    seconds = duration(clipped)
    return {"seconds": seconds, "total_seconds": total,
            "fraction": seconds / total if total else None,
            "gaps": subtract([bounds], clipped)}


def assert_normalized(intervals):
    previous = None
    for left, right in intervals:
        if left >= right or (previous is not None and left <= previous):
            raise ValueError("区间没有规范化")
        previous = right
    return True
