SELECT MIN(a01.c03) AS out_01,
       MIN(a08.c02) AS out_02
FROM
    t01 AS a01,
    t03 AS a02,
    t06 AS a03,
    t10 AS a04,
    t13 AS a05,
    t16 AS a06,
    t18 AS a07,
    t21 AS a08
WHERE a03.c03 ='[us]'
  AND a04.c02 ='character-name-in-title'
  AND a01.c02 = a07.c01
  AND a07.c01 = a02.c02
  AND a02.c03 = a08.c01
  AND a08.c01 = a06.c02
  AND a06.c03 = a04.c01
  AND a08.c01 = a05.c02
  AND a05.c03 = a03.c01
  AND a01.c02 = a02.c02
  AND a02.c03 = a05.c02
  AND a02.c03 = a06.c02
  AND a05.c02 = a06.c02;

